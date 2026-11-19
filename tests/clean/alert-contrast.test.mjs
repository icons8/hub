// Clean's red reads at 4.5:1 or more in both variants: white words on the red fill (--c-alert: the "waiting for an
// answer" pill, the list counter) and red words (--destructive) on the window and on the grey shell. The same checks as
// the red-contrast checks of the light-red PR, kept here because this change sets the red. No browser: the colors are
// read from palette.css and clean.css as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8");
const palette = fs.readFileSync(path.join(DIR, "palette.css"), "utf8");
const [light, dark] = palette.split(":root.th-clean.th-clean-dark");
const token = (block, name) => new RegExp(`${name}: (#[0-9a-f]{6})`, "i").exec(block)?.[1];
const pair = (name) => new RegExp(`${name}: light-dark\\((#[0-9a-f]{6}), (#[0-9a-f]{6})\\)`, "i").exec(css)?.slice(1);
const lin = (c) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const lum = (hex) => { const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)); return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b); };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)]; return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };

test("white on the red fill reads at 4.5:1 or more, light and dark", () => {
  const alert = pair("--c-alert");
  assert.ok(alert, "--c-alert is a light-dark pair");
  for (const red of alert) assert.ok(ratio("#ffffff", red) >= 4.5, `white on ${red}: ${ratio("#ffffff", red).toFixed(2)}`);
});

test("red words read at 4.5:1 or more on the window and on the shell, light and dark", () => {
  const [win, shell] = [token(light, "--background"), pair("--c-shell")];
  const [winDark] = [token(dark, "--background")];
  assert.ok(win && winDark && shell, "the window and shell colors are found");
  const [l, d] = [token(light, "--destructive"), token(dark, "--destructive")];
  for (const bg of [win, shell[0]]) assert.ok(ratio(l, bg) >= 4.5, `light ${l} on ${bg}: ${ratio(l, bg).toFixed(2)}`);
  for (const bg of [winDark, shell[1]]) assert.ok(ratio(d, bg) >= 4.5, `dark ${d} on ${bg}: ${ratio(d, bg).toFixed(2)}`);
});
