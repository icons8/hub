// The usage bars of Clean at the bottom of the sidebar (the "Usage bars" option, off by default): colours, old readings and the
// account line, as in the native app (LimitsBlock). No browser: usage.js is pure, the snapshots below have the shape the core
// sends (limits, account, accounts, budget), the rules of clean.css and the wiring in clean.js are read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const ROOT = path.join(import.meta.dirname, "..", "..");
const DIR = path.join(ROOT, "modules", "web", "static", "clean");
const { usageModel, emailSteps, left, WORDS } = await import(pathToFileURL(path.join(DIR, "usage.js")).href);
const { clock } = await import(pathToFileURL(path.join(DIR, "..", "js", "clock.js")).href);
const { countsDown, pctNum, pctText } = await import(pathToFileURL(path.join(DIR, "..", "js", "limitpct.js")).href);
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8").replace(/\r\n/g, "\n");   // a Windows checkout: CRLF
const main = fs.readFileSync(path.join(DIR, "clean.js"), "utf8");
// the page's words come from the catalogs (web.clean.usage_*, web.limits.name_*): here the Russian ones, as clean.js builds them
const ru = JSON.parse(fs.readFileSync(path.join(ROOT, "i18n", "ru.json"), "utf8"));
// the catalog key of each word: the window names are the limits' own, "accounts" is the settings tab's
const keyOf = (k) => ({ five_hour: "web.limits.name_five_hour", seven_day: "web.limits.name_week", seven_day_opus: "web.limits.name_week_opus",
  seven_day_sonnet: "web.limits.name_week_sonnet", accounts: "web.settings.tab.accounts", left: "web.limits.left" })[k] || `web.clean.usage_${k}`;
const RU = Object.fromEntries(Object.keys(WORDS).map((k) => [k, ru[keyOf(k)]]));
const words = RU;

const NOW = 2_000_000_000;
// a snapshot as the core sends it: limits for the current account
const snap = ({ five = 0.4, week = 0.5, status = "allowed", eco = false, ts = NOW - 60, pool, active = "work", extra = false } = {}) => ({
  limits: {
    status, account: active, ts,
    windows: { five_hour: { used: five, resets: NOW + 2 * 3600 }, seven_day: { used: week, resets: NOW + 3 * 86400 },
      ...(extra ? { seven_day_opus: { used: 0.31, resets: NOW + 3 * 86400 }, seven_day_sonnet: { used: 0.12, resets: NOW + 3 * 86400 } } : {}) },
  },
  account: { active },
  accounts: pool ? { on: true, pool } : { on: false, pool: [] },
  budget: eco ? { mode: "economy" } : null,   // limit saving: the core's budget.view, no key in normal mode
});
const tones = (m) => m.windows.filter((w) => !w.extra).map((w) => w.tone);

test("a Codex reading says the share left, as Codex's /status; a Claude one the share used", () => {
  const cx = snap({ five: 0.2, week: 0.53 });
  cx.limits.engine = "codex";
  const words = { ...RU, left: ru["web.limits.left"] };
  const [five, week] = usageModel(cx, { now: NOW, words }).windows;
  assert.equal(week.text, ru["web.limits.left"].replace("{pct}", "47"));
  assert.equal(five.text, ru["web.limits.left"].replace("{pct}", "80"));
  assert.ok(week.spoken.includes(ru["web.limits.left"].replace("{pct}", "47")));
  assert.equal(week.tone, "ok", "the colour still follows the share used");
  assert.equal(week.used, 0.53, "the bar is still the share used");
  const claude = usageModel(snap({ five: 0.2, week: 0.53 }), { now: NOW, words }).windows[1];
  assert.equal(claude.text, "53%");
  // the old wiring wrote the bare share used into the caption and the old-reading row
  assert.doesNotMatch(main, /`\$\{w\.pct\}%`/);
  assert.match(main, /limpct", pctNum\(used, down\)/, "the line: the number only, the footer has room for both windows");
  assert.match(main, /dataset\.full = `\$\{T\(\.\.\.FULL\[name\]\)\} \$\{pctText\(used, down, left\)\}`/, "the word in the tooltip");
});

test("colour: grey below 80%, orange 80–95%, red from 95% and when not working", () => {
  assert.deepEqual(tones(usageModel(snap({ five: 0.4, week: 0.79 }), { now: NOW, words })), ["ok", "ok"]);
  assert.deepEqual(tones(usageModel(snap({ five: 0.8, week: 0.944 }), { now: NOW, words })), ["warn", "warn"]);
  assert.deepEqual(tones(usageModel(snap({ five: 0.95, week: 1 }), { now: NOW, words })), ["high", "high"]);
  assert.deepEqual(tones(usageModel(snap({ five: 0.1, status: "rejected" }), { now: NOW, words })), ["high", "high"]);
});

test("colour in economy: red and orange stay, yellow only instead of grey", () => {
  assert.deepEqual(tones(usageModel(snap({ five: 0.97, week: 0.99, eco: true }), { now: NOW, words })), ["high", "high"], "97–99% in economy — red");
  assert.deepEqual(tones(usageModel(snap({ five: 0.95, week: 0.98, eco: true }), { now: NOW, words })), ["high", "high"], "95% in economy — red");
  assert.deepEqual(tones(usageModel(snap({ five: 0.85, week: 0.8, eco: true }), { now: NOW, words })), ["warn", "warn"], "80–95% in economy — orange");
  assert.deepEqual(tones(usageModel(snap({ five: 0.4, week: 0.79, eco: true }), { now: NOW, words })), ["eco", "eco"], "below 80% in economy — yellow");
  assert.deepEqual(tones(usageModel(snap({ five: 0.4, week: 0.2 }), { now: NOW, words })), ["ok", "ok"], "below 80% without economy — grey");
  const broken = usageModel(snap({ five: 0.1, week: 0.2, status: "rejected", eco: true }), { now: NOW, words });
  assert.equal(broken.broken, true);
  assert.deepEqual(tones(broken), ["high", "high"], "not working in economy — red");
  const s = snap({ five: 0.99, eco: true });
  s.limits.windows.five_hour.resets = NOW - 10;
  assert.equal(usageModel(s, { now: NOW, words }).windows[0].tone, "ok", "a window whose reset has passed — no colour, even in economy");
});

test("colour: a not-working account keeps a full-strength red bar even when the reading is old, as in the native app", () => {
  const calls = main.match(/usageBar\(w, [^)]*\)\)/g) || [];
  assert.equal(calls.length, 1, "the compact bars");
  assert.match(calls[0], /&& !m\.broken/);
  assert.match(main, /usageBar\(w, old && !m\.broken\)/, "the rows");
  const m = usageModel(snap({ five: 0.6, status: "rejected", ts: NOW - 3 * 3600 }), { now: NOW, words });
  assert.ok(m.stale && m.broken);
});

test("not working: only the hub's own refusal; an old reading is paler but not red", () => {
  const old = usageModel(snap({ five: 0.2, week: 0.3, ts: NOW - 2 * 3600 }), { now: NOW, words });
  assert.ok(old.stale);
  assert.equal(old.broken, false, "an old reading alone is not «not working»");
  assert.deepEqual(tones(old), ["ok", "ok"]);
  assert.equal(usageModel(snap({ five: 0.2, status: "rejected" }), { now: NOW, words }).broken, true);
});

test("percent and even-pace mark: the mark is how much of the window has passed", () => {
  const m = usageModel(snap({ five: 0.834 }), { now: NOW, words });
  const five = m.windows.find((w) => w.id === "five_hour"), week = m.windows.find((w) => w.id === "seven_day");
  assert.equal(five.pct, 83);
  assert.ok(Math.abs(five.even - 3 / 5) < 1e-9, "2 hours left of 5 — the mark at 60%");
  assert.ok(Math.abs(week.even - 4 / 7) < 1e-9, "3 days left of 7 — the mark at 4/7");
  assert.ok(five.spoken.startsWith(`${words.five_hour} 83%, ${words.resets.split("{when}")[0]}`), five.spoken);
});

test("a window whose reset has passed: no fill colour, no mark, its own words", () => {
  const s = snap({ five: 0.99 });
  s.limits.windows.five_hour.resets = NOW - 10;
  const w = usageModel(s, { now: NOW, words }).windows[0];
  assert.equal(w.lapsed, true);
  assert.equal(w.tone, "ok");
  assert.equal(w.even, null);
  assert.ok(w.spoken.includes(words.lapsed), w.spoken);
});

test("a fresh reading stays fresh when the link drops or the hub is quiet, as in the native app", () => {
  // the stream sends a snapshot only on a hub event: 15 quiet minutes or a reconnect blip are not an old reading
  assert.equal(usageModel(snap(), { now: NOW, words, linkDown: true }).stale, null, "link down, reading 1 min old — no clock");
  assert.equal(usageModel(snap(), { now: NOW, words, sinceSnap: 20 * 60 }).stale, null, "no hub event for 20 min — no clock");
  assert.equal(usageModel(snap({ ts: NOW - 50 * 60 }), { now: NOW, words, sinceSnap: 20 * 60 }).stale, null, "50 min old reading — still fresh");
});

test("the text line: yellow only while the core says economy (budget.mode), grey otherwise", () => {
  // limitsLine run on stub nodes: the snapshot has no `economy` key, the core sends `budget` (None in normal mode)
  const code = main.slice(main.indexOf("const SHORT = "), main.indexOf("// The usage bars"));
  const run = (s, pick = (k) => k.dataset.tone) => {
    let line = null;
    const el = (tag, cls, text) => { const n = { cls, text, dataset: {}, kids: [], classList: { add() {} }, append(...k) { this.kids.push(...k); },
      querySelectorAll: () => [], addEventListener() {} }; if (cls === "clean-lim") line = n; return n; };
    const root = { replaceChildren() {} };
    new Function("hub", "on", "T", "clock", "USAGE_WORDS", "countsDown", "pctNum", "pctText", `${code}; return limitsLine;`)(
      { el }, () => true, (k, fb) => fb, clock, WORDS, countsDown, pctNum, pctText)(root, s);
    return line.kids.filter((k) => k.cls === "limseg").map(pick);
  };
  assert.deepEqual(run(snap({ five: 0.4, week: 0.2, eco: true })), ["eco", "eco"], "economy — yellow");
  assert.deepEqual(run(snap({ five: 0.4, week: 0.2 })), ["ok", "ok"], "normal — grey");
  assert.deepEqual(run(snap({ five: 0.97, week: 0.85 })), ["high", "warn"], "normal — red and orange stay");
  // the percent: a Claude reading — the share used; a Codex one — the share left, as Codex's /status says it
  const pct = (k) => k.kids.find((x) => x.cls === "limpct").text;
  assert.deepEqual(run(snap({ five: 0.2, week: 0.53 }), pct), ["20%", "53%"]);
  const cx = snap({ five: 0.2, week: 0.53 });
  cx.limits.engine = "codex";
  // PR #783 review: "80% left" + "47% left" did not fit the 176 px footer, the week wrapped out of sight; the word is in the tooltip
  assert.deepEqual(run(cx, pct), ["80%", "47%"]);
  assert.deepEqual(run(cx, (k) => k.dataset.full), ["5 hours 80% left", "Week 47% left"]);
  assert.equal(pctNum(120, true), "0%", "never below zero");
  assert.deepEqual(run(cx), ["ok", "ok"], "the colour still follows the share used");
});

test("old reading: a clock hint with the time and the reason; the link to the hub goes first", () => {
  assert.equal(usageModel(snap(), { now: NOW, words }).stale, null, "fresh — no clock");
  const hourOld = usageModel(snap({ ts: NOW - 2 * 3600 }), { now: NOW, words });
  assert.equal(hourOld.stale.why, "rest");
  assert.ok(hourOld.stale.text.startsWith(words.at.split("{time}")[0]), hourOld.stale.text);
  assert.match(hourOld.stale.text.split("\n")[0], /\d/, "the time of the reading");
  const stale = snap({ ts: NOW - 2 * 3600 });
  assert.equal(usageModel(stale, { now: NOW, words, linkDown: true }).stale.why, "link");
  // the stream sends a snapshot only on a hub event: a quiet hub with a live link is not "no connection"
  assert.equal(usageModel(stale, { now: NOW, words, sinceSnap: 16 * 60 }).stale.why, "rest");
  assert.equal(usageModel(stale, { now: NOW, words, sinceSnap: null }).stale.why, "cache");
});

test("the account line: the e-mail only with a pool of 2+ accounts, ✓ when it is the active one", () => {
  const pool = [{ name: "work", email: "someone@example.com" }, { name: "spare", email: "other@example.com" }];
  const m = usageModel(snap({ pool }), { now: NOW, words });
  assert.equal(m.email, "someone@example.com");
  assert.equal(m.active, true);
  assert.equal(usageModel(snap({ pool: [pool[0]] }), { now: NOW, words }).email, "", "one account — nothing to tell apart");
  const other = snap({ pool });
  other.limits.account = "spare";
  assert.equal(usageModel(other, { now: NOW, words }).active, false);
});

test("the account line without an e-mail: the account name as the core gives it, the same link to the accounts settings", () => {
  const pool = [{ name: "work-a" }, { name: "token-b" }];
  const m = usageModel(snap({ pool, active: "work-a" }), { now: NOW, words });
  assert.equal(m.email, "");
  assert.equal(m.line, "work-a", "no e-mail in the pool — the name as it is, not shortened");
  assert.equal(usageModel(snap({ pool, active: "token-b" }), { now: NOW, words }).line, "token-b", "no prefix or suffix is cut");
  assert.equal(m.pool, true, "accounts on — the line is the link");
  const bad = usageModel(snap({ pool: [{ name: "work", email: "not a mail" }, { name: "spare" }] }), { now: NOW, words });
  assert.equal(bad.line, "work", "a broken e-mail counts as none");
  const mail = usageModel(snap({ pool: [{ name: "work", email: "someone@example.com" }, { name: "spare" }] }), { now: NOW, words });
  assert.equal(mail.line, "someone@example.com", "the e-mail when there is one");
  assert.equal(usageModel(snap({ pool: [pool[0]], active: "work-a" }), { now: NOW, words }).line, "", "one account — no line, as before");
  assert.equal(usageModel(snap(), { now: NOW, words }).line, "", "accounts off — no line, as before");
  const acc = main.slice(main.indexOf("const usageAcc = "), main.indexOf("const usageFoot = "));
  assert.match(acc, /hub\.el\("span", "cu-mail", m\.line\)/);
  assert.match(acc, /fitMail\(shown, m\.email \? emailSteps\(m\.email\) : \[m\.line\]\)/, "a name is not cut at its dot");
});

test("e-mail steps: whole, then without the top-level zone; the middle is never cut", () => {
  assert.deepEqual(emailSteps("name@company.example.com"), ["name@company.example.com", "name@company.example"]);
  assert.deepEqual(emailSteps("name@localhost"), ["name@localhost"]);
  for (const s of emailSteps("a.very.long.name@sub.example.org")) assert.ok(!s.includes("…"));
  assert.match(css, /\.cu-mail \{[^}]*text-overflow: ellipsis/, "the end is cut by CSS");
});

test("time to reset in the page locale; less than a minute — the catalog's «soon»", () => {
  assert.equal(left(NOW + 30, NOW, "ru", words), words.soon);
  assert.match(left(NOW + 4 * 3600, NOW, "en"), /4 hours/);
  assert.match(left(NOW + 23 * 3600 + 40 * 60, NOW, "en"), /1 day/);
  assert.match(left(NOW + 90 * 60, NOW, "es"), /2 horas/);
  const in2h = new Intl.RelativeTimeFormat("ru", { numeric: "always" }).format(2, "hour");
  assert.equal(usageModel(snap(), { now: NOW, words, locale: "ru" }).windows[0].reset, words.resets.replace("{when}", in2h));
});


test("nothing to show without windows", () => {
  assert.equal(usageModel({ limits: {} }, { now: NOW, words }), null);
  assert.equal(usageModel(null, { now: NOW, words }), null);
});

test("CSS: colours from the theme tokens, old reading paler, the mark and the hint", () => {
  assert.match(css, /\[data-tone="warn"\] \.cu-fill \{ background: var\(--c-warn-bar\)/);
  assert.match(css, /\[data-tone="high"\] \.cu-fill \{ background: var\(--destructive\)/);
  assert.match(css, /\[data-tone="eco"\] \.cu-fill \{ background: var\(--c-eco\)/);
  assert.match(css, /\.cu-fill\.old \{ opacity: 0\.4/);
  assert.match(css, /\.cu-mark \{[^}]*width: 1\.5px/);
  assert.match(css, /\.cu-hint\[hidden\] \{ display: none/);
  assert.match(css, /\.cu-seg\[aria-pressed="true"\]/);
  // hover only where there is a mouse
  const hovers = css.match(/\.cu-[a-z-]+[^{,]*:hover/g) || [];
  assert.ok(hovers.length > 0);
  const media = css.slice(css.indexOf("@media (hover: hover) and (pointer: fine) {\n  html.th-clean button.cu-acc:hover"));
  for (const h of hovers) assert.ok(media.includes(h), `${h} sits inside the hover media query`);
});

test("CSS: the bottom line sits level with ☰ (36 px, 40 on a phone); the text line stays for the option's off state", () => {
  assert.match(css, /\.clean-usage \{ --cu-row: 36px; --cu-pull: -48px;/);
  assert.match(css, /\.clean-usage \{ --cu-row: 40px; --cu-pull: -52px; \}/);
  // off (the default): the old text line's rules are as they were, and the new layout of the footer only with the class
  assert.match(css, /html\.th-clean \.aside-foot \{ min-height: 56px; padding: 12px 12px 8px calc\(var\(--clean-menu-x\) \+ var\(--clean-menu-w\)\); display: flex; align-items: center; justify-content: flex-end; \}/);
  assert.match(css, /html\.th-clean \.clean-lim \{ display: inline-flex;/);
  assert.match(css, /html\.th-clean\.th-clean-usage \.aside-foot \{ align-items: flex-end; \}/);
  assert.match(css, /html\.th-clean\.th-clean-usage \.aside-foot \.slot-sidebar-footer \{ display: block;/);
  assert.match(main, /localStorage\.getItem\(USAGE_KEY\)/, "the compact / expanded choice is kept");
});

test("the option: off by default, the class on <html> only while the theme is on, the old text line when off", () => {
  assert.match(main, /\{ key: "usage", on: false, label: T\("web\.clean\.usage_bars", "Usage bars"\), hint: T\("web\.clean\.usage_bars_hint", ""\) \}/);
  assert.match(main, /classList\.toggle\("th-clean-usage", usageBars\(\)\)/);
  assert.match(main, /const usageBars = \(\) => on\(\) && !!hub\.option\?\.\("usage"\);/);
  const slot = main.slice(main.indexOf('hub.addSlot("sidebar.footer"'));
  assert.match(slot, /if \(!usageBars\(\)\) \{[^}]*limitsLine\(root, snap\); return; \}/, "off — the old line");
  // the old line's code is the same as before the bars: its words, its tone, its link
  const old = main.slice(main.indexOf("const limitsLine = "), main.indexOf("// The usage bars"));
  for (const part of ['hub.el("span", "clean-lim")', 'seg.dataset.tone = tone(used)', 'snap?.budget?.mode === "economy"', 'line.classList.add("limlink")'])
    assert.ok(old.includes(part), part);
});

test("old reading: each row has its own clock and only the pressed one opens its hint", () => {
  assert.match(main, /usageClock\(m\.stale, w\.id\)/, "the clock knows its window");
  assert.match(main, /const shown = usage\.tip === b\.dataset\.id;/);
  assert.match(main, /b\.querySelector\("\.cu-hint"\)\.hidden = !shown;/);
  assert.doesNotMatch(main, /usage\.tip = (true|false)/, "no shared on/off flag for all clocks");
});

test("expanded view: the account e-mail is its own line as wide as the windows; the bottom line keeps only the switch", () => {
  assert.match(main, /if \(open && m\.line\) box\.append\(usageAcc\(m\)\);/);
  const foot = main.slice(main.indexOf("const usageFoot = "), main.indexOf("const usageRows = "));
  assert.doesNotMatch(foot, /cu-acc\b|cu-mail/, "the e-mail is not squeezed next to ☰ and the switch");
  assert.match(css, /\.cu-acc \{[^}]*margin-block: -4px; margin-inline: var\(--cu-pull\) 0;/, "the line starts under the windows, at their left edge");
  assert.match(css, /\.cu-foot\.open \{ justify-content: flex-end; \}/);
});

test("expanded view: the name stays on the left and the reset on the right; the percent joins the name", () => {
  const rows = main.slice(main.indexOf("const usageRows = "), main.indexOf("function usageDraw("));
  assert.match(rows, /hub\.el\("span", "cu-peek"\)/);
  assert.match(rows, /else if \(w\.reset\) line\.append\(hub\.el\("span", "cu-val", w\.reset\)\);/);
  assert.doesNotMatch(rows, /cu-tip/, "no number over the bar in the expanded view");
  assert.doesNotMatch(css, /\.cu-line\.peek > :not\(\.cu-tip\) \{ visibility: hidden/);
});

test("hover, tap and the clock's hint show and hide nodes that are on the page: no rebuild under the pointer", () => {
  // before, every hover rebuilt the whole block, and a hint could stay behind on a node the pointer had already left
  for (const fn of ["peekShow", "peekHide", "tipSet"]) {
    const body = main.slice(main.indexOf(`const ${fn} = `), main.indexOf(`const ${fn} = `) + 600).split("\n  const ")[0];
    assert.doesNotMatch(body, /usageDraw\(/, `${fn} does not rebuild`);
    assert.match(body, /usageShow\(\)/, `${fn} shows or hides`);
  }
  const draw = main.slice(main.indexOf("function usageDraw("), main.indexOf('hub.addSlot("sidebar.footer"'));
  assert.doesNotMatch(draw, /usage\.peek,|usage\.tip,/, "the hover is not part of what the block is rebuilt for");
  assert.match(draw, /host\?\.matches\(":hover, :focus"\)/, "a hover the new nodes do not confirm is dropped after a rebuild");
  assert.match(css, /\.cu-hint\[hidden\] \{ display: none/);
  // the hidden caption nodes are in the markup from the start
  assert.match(main, /const tip = hub\.el\("span", "cu-tip"\);\s*tip\.hidden = true;/);
});

test("keyboard: a rebuild's own focus and blur do not rebuild again, and the hide waits until the focus has moved", () => {
  // checked in Chromium: Tab onto a bar used to rebuild until "Maximum call stack size exceeded", and the next Tab dropped the
  // focus to the page instead of the second bar
  const peek = main.slice(main.indexOf("const wirePeek = "), main.indexOf("const usageClock = "));
  assert.match(peek, /addEventListener\("focus", \(\) => \{ if \(!usage\.drawing && peekable && usage\.peek !== w\.id/);
  assert.match(peek, /addEventListener\("blur", \(\) => \{ if \(!usage\.drawing\) setTimeout\(\(\) => peekHide\(w\.id\)\); \}\)/);
  const clock = main.slice(main.indexOf("const usageClock = "), main.indexOf("const usageSwitch = "));
  assert.match(clock, /addEventListener\("blur", \(\) => \{ if \(!usage\.drawing\) setTimeout\(\(\) => tipSet\(id, false\)\); \}\)/);
  const draw = main.slice(main.indexOf("function usageDraw("), main.indexOf('hub.addSlot("sidebar.footer"'));
  assert.match(draw, /usage\.drawing = true;\s*try \{\s*root\.replaceChildren\(box\);[\s\S]*?\} finally \{ usage\.drawing = false; \}/);
});

test("the weekly Opus and Sonnet windows are not lost: rows in the expanded view, the week bar's hint in the compact one", () => {
  const m = usageModel(snap({ extra: true }), { now: NOW, words });
  assert.deepEqual(m.windows.map((w) => [w.id, w.extra]), [["five_hour", false], ["seven_day", false], ["seven_day_opus", true], ["seven_day_sonnet", true]]);
  const opus = m.windows[2];
  assert.equal(opus.name, words.seven_day_opus);
  assert.equal(opus.pct, 31);
  assert.ok(Math.abs(opus.even - 4 / 7) < 1e-9, "a weekly window: the same span as the week");
  assert.deepEqual(usageModel(snap(), { now: NOW, words }).windows.map((w) => w.id), ["five_hour", "seven_day"], "none sent — two");
  // only Opus without the two main windows is nothing to show
  const only = snap();
  delete only.limits.windows.five_hour; delete only.limits.windows.seven_day;
  only.limits.windows.seven_day_opus = { used: 0.4, resets: NOW + 86400 };
  assert.equal(usageModel(only, { now: NOW, words }), null);
  const foot = main.slice(main.indexOf("const usageFoot = "), main.indexOf("const usageRows = "));
  assert.match(foot, /m\.windows\.filter\(\(w\) => !w\.extra\)/, "two bars");
  assert.match(foot, /if \(said\.length > 1\) b\.title = said\.join\("\\n"\);/, "the hint of the week bar");
  assert.match(main, /for \(const w of m\.windows\) \{\s*const row = hub\.el\("button", "cu-row"\);/, "the expanded view has every window");
});

test("the compact view keeps the way to Settings → Accounts when accounts are on", () => {
  const foot = main.slice(main.indexOf("const usageFoot = "), main.indexOf("const usageRows = "));
  assert.match(foot, /if \(m\.pool\) \{[^}]*cu-accbtn[\s\S]*?hub\.openSettings\?\.\("accounts"\)/);
  assert.match(css, /\.cu-accbtn::before \{[^}]*url\("account\.svg"\)/);
  assert.equal(usageModel(snap(), { now: NOW, words }).pool, false, "accounts off — no button");
  assert.equal(usageModel(snap({ pool: [{ name: "work" }] }), { now: NOW, words }).pool, true);
});

test("nothing is read from a module's own key, and no machine is named in the words", () => {
  assert.doesNotMatch(main + fs.readFileSync(path.join(DIR, "usage.js"), "utf8"), /accounts_live/);
  for (const [k, v] of Object.entries(WORDS)) assert.doesNotMatch(v, /\bMac\b|macOS|Terminal/, k);
  const en = JSON.parse(fs.readFileSync(path.join(ROOT, "i18n", "en.json"), "utf8"));
  for (const k of ["auth", "token", "net", "quiet", "machine"]) assert.equal(en[`web.clean.usage_${k}`], undefined, `usage_${k} is gone from the catalogs`);
});

test("every word of the block comes from the catalogs, in English and Russian, with the placeholders usage.js fills", () => {
  const en = JSON.parse(fs.readFileSync(path.join(ROOT, "i18n", "en.json"), "utf8"));
  for (const k of Object.keys(WORDS)) {
    const key = keyOf(k);
    assert.ok(main.includes(`T("${key}", USAGE_WORDS.${k}`), `clean.js asks the catalog for ${key}`);
    assert.equal(en[key], WORDS[k], `${key}: the fallback in usage.js is the English of the catalog`);
    assert.ok(typeof ru[key] === "string" && ru[key], `${key} in Russian`);
    for (const ph of WORDS[k].match(/\{\w+\}/g) || []) assert.ok(ru[key].includes(ph), `${key}: Russian keeps ${ph}`);
  }
  for (const key of ["web.clean.usage_bars", "web.clean.usage_bars_hint"]) assert.ok(en[key] && ru[key], key);
});

test("the view switch icons come from the theme's Icons8 pack, none drawn by hand", () => {
  const icons = JSON.parse(fs.readFileSync(path.join(DIR, "icons8.json"), "utf8")).icons;
  for (const name of ["bars", "list"]) {
    assert.match(css, new RegExp(`\\.cu-seg-${name}::before \\{[^}]*url\\("${name}\\.svg"\\)`));
    assert.ok(icons.items[name]?.id, `${name}.svg is listed in icons8.json with its Icons8 id`);
    const svg = fs.readFileSync(path.join(DIR, `${name}.svg`), "utf8");
    assert.match(svg, /viewBox="0 0 24 24"/, `${name}.svg keeps the pack's 24 grid, as the other Forma icons`);
  }
  assert.equal(icons.items.bars.commonName, "equal-sign");
});
