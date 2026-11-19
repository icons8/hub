// A voice recording keeps the field in the flow: an absolute field lay over the reply bar and the file chips, and the
// recording rules on the row of buttons also hit the reply bar ("Reply to ...") above the field.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8");
// every rule as {sel, body}; at-rule wrappers (@media, @supports) are flattened, @keyframes are skipped
const rules = [];
const flat = css.replace(/\/\*[\s\S]*?\*\//g, "");
for (const m of flat.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
  const sel = m[1].trim();
  if (sel.startsWith("@") || /^(from|to|\d+%)/.test(sel)) continue;
  rules.push({ sel, body: m[2] });
}
const recording = (sel) => /\.composer\.rec\b/.test(sel) || /form\.composer\.rec\)/.test(sel);

test("while recording, the field is not taken out of the card", () => {
  const prompt = rules.filter((r) => recording(r.sel) && /> \.prompt\b/.test(r.sel));
  assert.ok(prompt.length, "the field has a rule while recording");
  for (const r of prompt) assert.doesNotMatch(r.body, /position:\s*(absolute|fixed)/, r.sel);
  assert.doesNotMatch(css, /clean-prompt-out/, "the fade-out of an absolute field is gone");
});

test("the recording rules on the bar of buttons leave the reply bar alone", () => {
  const bars = rules.filter((r) => recording(r.sel) && /\.bar(?![\w-])/.test(r.sel) && !/\.bar\s*>|\.bar::/.test(r.sel));
  assert.ok(bars.length >= 6, "the recording row rules are there");
  for (const r of bars) for (const sel of r.sel.split(",")) {
    if (recording(sel) && /\.bar(?![\w-])/.test(sel) && !/\.bar\s*>|\.bar::/.test(sel)) {
      assert.match(sel, /\.bar:not\(\.reply-bar\)/, `a rule on .bar must skip .reply-bar: ${sel.trim()}`);
    }
  }
});

test("the recording row is one row of the card (the field above it is not overlaid by a second row)", () => {
  for (const r of rules.filter((r) => recording(r.sel) && /grid-template-rows/.test(r.body))) {
    assert.doesNotMatch(r.body, /grid-template-rows:\s*28px/, r.sel);
    const track = r.body.match(/grid-template-rows:\s*([^;]+);/)[1].trim();
    assert.match(track, /^(var\(--row, \d+px\)|\d+px)$/, `one row only: ${r.sel}`);
  }
  for (const r of rules.filter((r) => recording(r.sel) && /\.recstrip > /.test(r.sel) && /grid-area/.test(r.body))) {
    assert.doesNotMatch(r.body, /grid-area:\s*2\s*\/\s*[2-5]\b(?!\s*\/)/, `a second grid row is gone: ${r.sel}`);
  }
});
