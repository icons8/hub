// The bottom row of the composer card: nothing overlaps at any width and with any number of plugin buttons.
// No browser: the order of steps is the pure function pickStep, the row measuring is fit.js on stub nodes whose widths depend
// on the classes on .dock-inner (as in clean.css), and the rules of clean.css are read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const { pickStep, STEPS, GAP, MIN, mountFit } = await import(pathToFileURL(path.join(DIR, "fit.js")).href);
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8");

// widths of the status buttons: executors, model, effort — with full and with short labels
const W = { long: { par: 150, mod: 90, eff: 70 }, short: { par: 110, mod: 70, eff: 70 } };
const total = (w, names) => names.reduce((a, k) => a + w[k], 0) + GAP * (names.length - 1);

test("the order of steps: labels, ellipsis, effort, model, executors — each next one only when the previous does not fit", () => {
  assert.equal(pickStep(W, total(W.long, ["par", "mod", "eff"])), 0);
  assert.equal(pickStep(W, total(W.long, ["par", "mod", "eff"]) - 1), 1);
  assert.equal(pickStep(W, total(W.short, ["par", "mod", "eff"]) - 1), 2);
  const floor = (names) => names.reduce((a, k) => a + Math.min(W.short[k], MIN[k]), 0) + GAP * (names.length - 1);
  assert.equal(pickStep(W, floor(["par", "mod", "eff"]) - 1), 3);
  assert.equal(pickStep(W, floor(["par", "mod"]) - 1), 4);
  assert.equal(pickStep(W, floor(["par"]) - 1), 5);
  assert.equal(pickStep(W, 0), 5);
  const rooms = [];
  for (let r = 400; r >= 0; r--) rooms.push(pickStep(W, r));
  assert.deepEqual(rooms, [...rooms].sort((a, b) => a - b), "the less room, the more steps — they never go back");
});

test("a session without effort takes the same steps without its width", () => {
  const w = { long: { par: 150, mod: 90 }, short: { par: 110, mod: 70 } };
  assert.equal(pickStep(w, 150 + GAP + 90), 0);
  assert.equal(pickStep(w, 0), 5);
  assert.equal(pickStep({ long: {}, short: {} }, 0), 0, "no status buttons — nothing to give way");
});

test("steps are cumulative class lists named in clean.css", () => {
  for (let i = 1; i < STEPS.length; i++) assert.deepEqual(STEPS[i].slice(0, STEPS[i - 1].length), STEPS[i - 1]);
  for (const n of ["short", "shrink", "noeff", "nomod", "nopar"]) assert.match(css, new RegExp(`\\.dock-inner\\.clean-fit-${n}\\b`), `clean.css has no rule for clean-fit-${n}`);
  assert.match(css, /\.dock-inner\.clean-up\b/);
});

test("clean.css: 8 between neighbours, the status room comes from the measure, the camera is 8 from the mic", () => {
  // --fit-l is the line start (the "+" side), --fit-r its end: logical, so fit.js measures along the line (rtl too)
  assert.match(css, /padding-block: 0; padding-inline: var\(--fit-l, 52px\) var\(--fit-r, [^;]*\);\s*justify-content: flex-end; align-items: center; gap: 8px;/);
  // sides are logical (line start and end), so a right-to-left page mirrors the row
  assert.match(css, /inset-inline-start: auto; inset-inline-end: calc\(12px \+ var\(--row, 32px\) \+ 8px\); bottom: 18px; \}/, "camera: 12 + the one core button + 8 from the line end, the same with an empty field and with text");
  assert.doesNotMatch(css, /translateX\(calc\(var\(--row, 32px\) \+ 8px\)\)/, "the camera does not move when typing swaps the mic for send");
  assert.match(css, /\.bar:not\(\.reply-bar\) \{ flex-wrap: nowrap; gap: 8px; \}/);
  assert.match(css, /\.bar \{ padding-inline-start: calc\(var\(--row, 32px\) \+ 8px\); \}/, "8 from \"+\" to the first button of the row");
  assert.match(css, /\.bar:not\(\.reply-bar\) > :is\(\.mic, \.send\) \{ margin-inline-start: calc\(var\(--row, 32px\) \+ 8px\); \}/, "room for the camera before the mic or send");
  assert.doesNotMatch(css, /(?<![-\w])(padding|margin)-(left|right): calc\(var\(--row/);
  // container steps do not decide for the room: short labels and hiding come from fit.js only
  assert.doesNotMatch(css, /@container status \([^)]*\)\s*\{[^}]*(par-long|\.stat-eff|\.stat-mod)/);
});

// A page stub: a tree of nodes with rectangles. The width of the status line and the place of the buttons on the right follow
// the classes on .dock-inner the way clean.css does; the plugin buttons wrap (clean-up) when they do not fit.
function page({ cardW = 366, tools = 0, vf = true, rtl = false, dict = false, spec = false, tab = true }) {
  const d = { cls: new Set(), style: {}, dataset: {} };
  d.classList = { add: (c) => d.cls.add(c), remove: (c) => d.cls.delete(c), toggle: (c, on) => (on ? d.cls.add(c) : d.cls.delete(c)), contains: (c) => d.cls.has(c) };
  d.style = { props: {}, setProperty(k, v) { this.props[k] = v; }, removeProperty(k) { delete this.props[k]; } };
  const up = () => d.cls.has("clean-up");
  const BTN = 32, G = 8, PAD = 12;
  const rect = (left, w, top = 100, h = BTN) => ({ left, right: left + w, top, bottom: top + h, width: w, height: h });
  // right to left the page shows the row mirrored: "+" at the right edge, the buttons from the left (the stub lays it out
  // left to right and mirrors only what the page measures)
  const flip = (r) => (rtl ? { ...r, left: cardW - r.right, right: cardW - r.left } : r);
  const rowTop = 100, upTop = 60;
  const el = (r, shown = true, cls = "", position = "static") => ({ getBoundingClientRect: () => flip(r()), getClientRects: () => (shown ? [1] : []), classList: { contains: (c) => c === cls }, style: {}, position });
  const toolsW = () => (tools ? tools * BTN + (tools - 1) * G : 0);
  const right0 = () => cardW - PAD;                                   // the right edge of the content
  const send = () => rect(right0() - BTN, BTN, rowTop);                // the one core button: the mic in an empty field, send with text
  const dictR = () => rect(send().left - G - BTN, BTN, rowTop);          // the dictation button next to send, while the field has text
  const camL = () => (vf ? send().left - G - BTN : null);
  const toolsR = () => {
    const last = (dict ? dictR().left : vf ? camL() : send().left) - G;
    return up() ? rect(right0() - toolsW(), toolsW(), upTop) : rect(last - toolsW(), toolsW(), rowTop);
  };
  const need = BTN + G + toolsW() + G + (vf ? BTN + G : 0) + (dict ? BTN + G : 0) + BTN;   // '+', buttons, camera, mic or send
  const natural = (long, short) => () => rect(0, d.cls.has("clean-fit-short") ? short : long, rowTop, 28);
  const chips = { par: natural(150, 110), mod: natural(90, 70), eff: natural(70, 70) };
  // the "specs" mark stands in the line between the executors and the model: a fixed 41 px that the three buttons do not measure
  const status = { getBoundingClientRect: () => flip(rect(0, cardW, rowTop)), classList: { contains: () => false }, style: {},
    // the tab (stat-left) is out of the flow and the limits line is not shown: neither takes room
    children: [el(chips.par, true, "stat-par"), ...(spec ? [el(() => rect(0, 41, rowTop, 20), true, "stat-spec")] : []), el(chips.mod, true, "stat-mod"), el(chips.eff, true, "stat-eff"),
      ...(tab ? [el(() => rect(24, cardW - 48, rowTop - 30, 24), true, "stat-left", "absolute"), el(() => rect(0, 90, rowTop), false, "limline")] : [])],
    querySelector: (q) => { const k = /stat-(\w+)/.exec(q)?.[1]; return k ? el(chips[k]) : null; } };
  const plus = el(() => rect(PAD, BTN, rowTop));
  // fit.js reads "does the row not fit" off the children: the one child here is as wide as the row needs, from the left edge of the content
  // scrollWidth is 4 over clientWidth always: the 44 px tap area of send on a touch screen; fit.js must not take it for a row that does not fit
  const bar = { scrollWidth: cardW - 2 * PAD + 4, clientWidth: cardW - 2 * PAD, classList: { contains: () => false },
    getBoundingClientRect: () => flip(rect(PAD, cardW - 2 * PAD, rowTop)),
    children: [el(() => rect(PAD, Math.max(need, cardW - 2 * PAD), rowTop))],
    querySelectorAll: (q) => [
      el(send), ...(dict && q.includes(":scope > .dictate") ? [el(dictR)] : []), ...(tools ? [el(toolsR)] : []),
    ] };
  const card = { classList: { contains: () => false }, getBoundingClientRect: () => flip(rect(0, cardW, 90, 80)),
    querySelector: (q) => (q.includes(".bar") ? bar : q.includes(".attach") ? plus : null) };
  d.querySelector = (q) => (q.includes("form.composer") ? card : q.includes(".status") ? status : null);
  d.querySelectorAll = (q) => (vf && q.includes(".vf-slot") ? [el(() => rect(camL(), BTN, rowTop))] : []);
  const doc = { querySelector: (q) => (q === ".dock-inner" ? d : null), fonts: null };
  return { d, doc, up, toolsR, camL };
}

async function fitted(opts) {
  const p = page(opts);
  let frame = null;
  Object.assign(globalThis, {
    document: p.doc, requestAnimationFrame: (f) => { frame = f; },
    ResizeObserver: class { observe() {} disconnect() {} }, MutationObserver: class { observe() {} disconnect() {} },
    CSS: { supports: () => true }, getComputedStyle: (e) => ({ direction: opts.rtl ? "rtl" : "ltr", position: e?.position }),
  });
  const fit = mountFit(() => true);
  fit.hook();
  frame?.();
  return p;
}

test("fit.js: right to left the row is measured along the line — the same steps and the same room as left to right", async () => {
  for (const tools of [0, 2, 5]) {
    for (const cardW of [1100, 700, 480, 366]) {
      const ltr = await fitted({ cardW, tools }), rtl = await fitted({ cardW, tools, rtl: true });
      assert.deepEqual([rtl.d.dataset.fit, rtl.up(), rtl.d.style.props["--fit-l"], rtl.d.style.props["--fit-r"]],
        [ltr.d.dataset.fit, ltr.up(), ltr.d.style.props["--fit-l"], ltr.d.style.props["--fit-r"]], `${tools} buttons at ${cardW}`);
    }
  }
});

test("fit.js: a wide window — everything in place, no wrap", async () => {
  const p = await fitted({ cardW: 1100, tools: 3 });
  assert.equal(p.d.dataset.fit, "0");
  assert.equal(p.up(), false);
});

test("fit.js: the status gives way by the real width, not by a button count", async () => {
  for (const tools of [0, 1, 3, 5]) {
    const row = { single: [], up: [] };
    for (let w = 1100; w >= 340; w -= 10) { const p = await fitted({ cardW: w, tools }); row[p.up() ? "up" : "single"].push(+p.d.dataset.fit); }
    for (const [mode, steps] of Object.entries(row)) assert.deepEqual(steps, [...steps].sort((x, y) => x - y), `${tools} buttons, ${mode}: the narrower the window, the more steps`);
    assert.equal(row.single[0], 0, `${tools} buttons: at 1100 everything is in place`);
  }
  // at the same width more plugin buttons mean no fewer steps
  for (const w of [700, 560, 480]) {
    const a = await fitted({ cardW: w, tools: 1 }), b = await fitted({ cardW: w, tools: 5 });
    assert.ok(+b.d.dataset.fit >= +a.d.dataset.fit || b.up(), `${w}: five buttons give way no less than one, or go above`);
  }
});

test("fit.js: the \"specs\" mark in the line takes its room off the buttons: the status gives way sooner, never later", async () => {
  let sooner = 0;
  for (const tools of [0, 1, 3]) for (let w = 1100; w >= 340; w -= 10) {
    const a = await fitted({ cardW: w, tools }), b = await fitted({ cardW: w, tools, spec: true });
    if (!a.up() && !b.up()) assert.ok(+b.d.dataset.fit >= +a.d.dataset.fit, `${w}/${tools}: a mark never lets the buttons keep more`);
    if (!a.up() && !b.up() && +b.d.dataset.fit > +a.d.dataset.fit) sooner++;
    if (b.d.dataset.fit === "5") assert.ok(b.d.cls.has("clean-fit-nopar"), `${w}/${tools}: at the last step the class that hides the mark is set`);
  }
  assert.ok(sooner > 0, "somewhere the mark makes a step needed that the line without it did not");
});

test("fit.js: the mark takes exactly its width and one gap off the room: the card must be 41 + 8 px wider for the full labels", async () => {
  const narrowest = async (spec) => { for (let w = 1100; w >= 300; w--) if ((await fitted({ cardW: w, spec })).d.dataset.fit !== "0") return w + 1; };
  assert.equal((await narrowest(true)) - (await narrowest(false)), 41 + GAP);
});

test("fit.js: the tab and a part that is not shown take no room from the buttons", async () => {
  for (const w of [700, 560, 480, 400, 366]) {
    const a = await fitted({ cardW: w, tab: false }), b = await fitted({ cardW: w });
    assert.deepEqual([b.d.dataset.fit, b.up(), b.d.style.props["--fit-l"], b.d.style.props["--fit-r"]], [a.d.dataset.fit, a.up(), a.d.style.props["--fit-l"], a.d.style.props["--fit-r"]], `${w}`);
  }
});

test("clean.css: the \"specs\" mark stands over the card like the buttons, and goes where they go: recording and the last step", () => {
  assert.match(css, /\.status > :is\(\.stat-par, \.stat-spec, \.stat-mod, \.stat-eff\) \{ position: relative; z-index: 2;/);
  assert.match(css, /form\.composer\.rec\) \.status > :is\(\.stat-par, \.stat-spec, \.stat-mod, \.stat-eff\) \{ display: none; \}/);
  assert.match(css, /\.vf-work\)\) \.status > :is\(\.stat-par, \.stat-spec, \.stat-mod, \.stat-eff\) \{ display: none; \}/);
  assert.match(css, /clean-fit-nopar \.status > :is\(\.stat-par, \.stat-spec\) \{ display: none; \}/);
});

test("fit.js: plugin buttons with no room in one row go above, and the status stays", async () => {
  const p = await fitted({ cardW: 366, tools: 5 });
  assert.equal(p.up(), true, "five buttons at 366 px — a row above");
  assert.ok(+p.d.dataset.fit < 5, "the status is visible in the bottom row");
  assert.match(p.d.style.props["--fit-l"], /^\d+px$/);
  assert.match(p.d.style.props["--fit-r"], /^\d+px$/);
});

test("fit.js: the status room is between '+' and the nearest button on the right, 8 off each", async () => {
  const p = await fitted({ cardW: 900, tools: 3 });
  const L = parseFloat(p.d.style.props["--fit-l"]), R = parseFloat(p.d.style.props["--fit-r"]);
  assert.equal(L, 12 + 32 + GAP, "left — '+' and the gap");
  assert.equal(900 - R, p.toolsR().left - GAP, "right — the plugin buttons and the gap");
});

test("fit.js: at the narrowest widths the row holds — the status goes away entirely only when there is no room at all", async () => {
  for (const tools of [0, 1, 3, 5, 9]) for (const w of [320, 360, 390, 480]) {
    const p = await fitted({ cardW: w, tools });
    assert.ok(["0", "1", "2", "3", "4", "5"].includes(p.d.dataset.fit), `${w}/${tools}`);
    const L = parseFloat(p.d.style.props["--fit-l"]), R = parseFloat(p.d.style.props["--fit-r"]);
    assert.ok(L + R <= w, `${w}/${tools}: the status paddings are within the card width`);
  }
});

test("fit.js: the theme is switched off — classes and paddings are removed", async () => {
  const p = await fitted({ cardW: 390, tools: 5 });
  assert.ok(p.d.cls.size > 0);
  let on = false;
  Object.assign(globalThis, { document: p.doc });
  const fit = mountFit(() => on);
  fit.reset();
  assert.equal(p.d.cls.size, 0);
  assert.equal(p.d.dataset.fit, undefined);
  assert.deepEqual(p.d.style.props, {});
});

test("clean.css: the reply is a card the whole width of the field: a quote in up to two lines, a round ✕ in its corner", () => {
  const rule = (sel) => css.match(new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + " \\{([^}]*)\\}"))?.[1] ?? "";
  const bar = rule("html.th-clean :is(.composer, .dock-inner:has(.vf-slot) form.composer) > .bar.reply-bar");
  assert.match(bar, /width: 100%/);
  assert.match(bar, /background: var\(--c-fill\)/);
  assert.match(bar, /padding-block: 0; padding-inline: 8px 0/, "the core's 36px on the right would push the ✕ off the edge of the card");
  assert.doesNotMatch(bar, /(^|[^-])height:/, "a two-line quote grows the card");
  const chip = rule("html.th-clean .composer .reply-chip");
  assert.match(chip, /flex: 1 1 0/);
  assert.match(chip, /text-overflow: ellipsis/);
  assert.match(chip, /background: none/, "the card is the bar, not the chip");
  assert.match(rule("html.th-clean .composer .reply-bar.quote .reply-chip"), /white-space: normal/, "the quote wraps");
  // the ✕ is the core's disc in the corner (dock.css): it does not shrink under a long quote and the quote keeps room for it
  const dock = fs.readFileSync(path.join(DIR, "..", "css", "dock.css"), "utf8");
  assert.match(dock, /\.composer :is\(\.pchip, \.reply-bar\) > \.x \{[^}]*flex: none; box-sizing: border-box; width: var\(--x-disc\)/, "the ✕ does not shrink under a long quote");
  assert.match(rule("html.th-clean :is(.composer, .dock-inner:has(.vf-slot) form.composer) > .bar.reply-bar.quote"), /padding-inline-end: var\(--x-room\)/, "a long quote does not run under the ✕");
  assert.doesNotMatch(rule("html.th-clean .composer .reply-bar > .x"), /width|height/, "the disc's size is the core's");
});

test("clean.css: the ✕ disc of a card in the field is flat like the other close buttons, 12 glyph, 2px key ring; text is Clean's", () => {
  const rule = (sel) => css.match(new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + " \\{([^}]*)\\}"))?.[1] ?? "";
  const x = rule("html.th-clean .composer :is(.pchip, .reply-bar) > .x");
  assert.match(x, /background: var\(--c-badge\)/, "a theme token, not the core's translucent page color");
  assert.match(x, /box-shadow: none/, "no shadow at rest");
  assert.match(x, /backdrop-filter: none/, "no blur");
  assert.match(rule("html.th-clean .composer :is(.pchip, .reply-bar) > .x .icon"), /width: 12px; height: 12px/, "the glyph is one of 12/16/20");
  assert.match(css, /\.board-card, \.composer :is\(\.pchip, \.reply-bar\) > \.x\):focus-visible \{\s*outline: 2px solid var\(--c-ring\); outline-offset: 2px; \}/, "the key ring is the theme's");
  assert.match(rule("html.th-clean .composer .reply-title"), /font-weight: 500/, "400 and 500 only");
  assert.match(rule("html.th-clean .pchip"), /font: 400 var\(--c-fs-s\)\/var\(--c-lh-s\) var\(--font\)/, "chips are 12/16");
  assert.doesNotMatch(rule("html.th-clean .pchip .ptile"), /font|border: 1px/, "the tile is a thumbnail: no frame, the core's miniature text");
  const hover = css.slice(css.indexOf("html.th-clean .composer :is(.pchip, .reply-bar) > .x:hover") - 60, css.indexOf("html.th-clean .composer :is(.pchip, .reply-bar) > .x:hover"));
  assert.match(hover, /@media \(hover: hover\) and \(pointer: fine\)/, "hover only for a mouse");
});

test("fit.js: the dictation button next to send takes room from the status; the 4 px tap area of send does not wrap the row", async () => {
  for (const w of [1100, 700, 480, 390]) {
    const a = await fitted({ cardW: w, vf: false }), b = await fitted({ cardW: w, vf: false, dict: true });
    assert.equal(b.up(), false, `${w}: a row that fits stays in one row`);
    assert.equal(parseFloat(b.d.style.props["--fit-r"]) - parseFloat(a.d.style.props["--fit-r"]), 32 + GAP, `${w}: the status ends before the dictation button`);
  }
  const p = await fitted({ cardW: 366, tools: 5, vf: false, dict: true });
  assert.equal(p.up(), true, "the buttons that do not fit still go above");
});
