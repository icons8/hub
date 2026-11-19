// On/off settings in Clean are toggles (clean.css): the core's "Enable / Disable" button of a module, a plugin and "External hubs"
// is drawn as a 32×18 track with a knob, and the checkboxes on "Look" (.feed-opts) and the accounts gateway (.ac-gw) stand as a
// toggle to the right of their name. The Clean options' hints say what happens when a toggle is off, not "unchecked".
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";

const ROOT = path.join(import.meta.dirname, "..", "..");
const css = readFileSync(path.join(ROOT, "modules", "web", "static", "clean", "clean.css"), "utf8");
const en = JSON.parse(readFileSync(path.join(ROOT, "i18n", "en.json"), "utf8"));
const rule = (sel) => {
  const i = css.indexOf(sel + " {");
  assert.ok(i >= 0, "no rule for " + sel);
  return css.slice(i, css.indexOf("}", i));
};

test("the enable button of a module, a plugin and External hubs is a 32×18 track, its label hidden from the eye", () => {
  const i = css.indexOf("html.th-clean :is(.module-head > .module-toggle,");
  assert.ok(i >= 0);
  const body = css.slice(i, css.indexOf("}", i));
  assert.match(body, /\.bus-head > \.btn\)/);
  assert.match(body, /width: 32px/);
  assert.match(body, /height: 18px/);
  assert.match(body, /font-size: 0/);
  assert.match(css, /\.module-toggle\.primary, \.plugin-row\.off > [^{]*\{\s*--tg: var\(--c-line-strong\); --tg-x: 0px; \}/, "off: a gray track, the knob on the left");
});

test("a toggle in progress keeps its knob whole: a half-strength track, no layer opacity", () => {
  const busy = css.slice(css.indexOf(':is(:disabled, .module-card[data-state="wait"] *) {'));
  assert.match(busy.slice(0, busy.indexOf("}")), /opacity: 1; cursor: progress; --tg: color-mix\(in oklch, var\(--tg-on, var\(--foreground\)\) 40%, var\(--background\)\)/);
  assert.match(css, /\.module-toggle\.primary, \.plugin-row\.off > [^{]*\{ --tg-on: var\(--c-line-strong\); \}/, "off in progress: half of the gray track");
  const off = rule('html.th-clean :is(.ac-gw, .feed-opts) > .opt > input[type="checkbox"]:disabled');
  assert.match(off, /opacity: 1; background: var\(--c-line\)/);
  assert.match(rule('html.th-clean .ac-gw:has(> .ac-gw-state[data-level="warn"]) > .opt > input[type="checkbox"]'), /cursor: progress/);
  assert.doesNotMatch(css, /input\[type="checkbox"\][^{]*\{[^}]*opacity: 0\.4/);
});

test("checkboxes on Look and the gateway are a toggle to the right of the name", () => {
  assert.match(rule("html.th-clean :is(.ac-gw, .feed-opts) > .opt"), /grid-template-columns: minmax\(0, 1fr\) auto/);
  const box = rule('html.th-clean :is(.ac-gw, .feed-opts) > .opt > input[type="checkbox"]');
  assert.match(box, /grid-column: 2/);
  assert.match(box, /appearance: none/);
  assert.match(rule('html.th-clean :is(.ac-gw, .feed-opts) > .opt > input[type="checkbox"]:checked'), /background: var\(--foreground\)/);
});

test("the Clean options' hints say Off, not Unchecked", () => {
  const hints = Object.entries(en).filter(([k]) => /^web\.clean\.\w+_hint$/.test(k) && k !== "web.clean.options_hint");
  assert.ok(hints.length >= 6);
  for (const [k, v] of hints) assert.doesNotMatch(v, /unchecked|untick/i, k);
});
