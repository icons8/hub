// The filter tabs above the list ("All", "Now") and the group headers ("Specialists") carry no number in Clean unless
// "Counts in the list" is ticked: a count of everyone is decoration that competes with the one number that asks for
// something — the red "waiting for you" counter on a row. The core still writes "All · 5" into the tab and a .gcount into
// the header (other themes show them); Clean keeps the word of the tab and hides the group count, and the option brings
// both back. clean.js and clean.css are read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8");
const js = fs.readFileSync(path.join(DIR, "clean.js"), "utf8");
const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");   // comments off: a rule's selector is what stands before its "{"
const rules = (sel) => [...bare.matchAll(/(^|\n)([^{}\n][^{}]*)\{([^}]*)\}/g)]
  .filter((m) => m[2].split(",").some((s) => s.trim() === sel)).map((m) => m[3]);

test("the option is off by default and sets a class on <html> only while Clean is chosen", () => {
  assert.match(js, /key: "counts", on: false/);
  assert.match(js, /classList\.toggle\("th-clean-counts", on\(\) && !!hub\.option\?\.\("counts"\)\)/);
});

test("the filter tabs keep only their word unless counts are on; the badge is made only with them", () => {
  const body = js.slice(js.indexOf("const splitTabs"), js.indexOf("\n  };", js.indexOf("const splitTabs")));
  assert.ok(body.length > 20, "splitTabs is there");
  assert.match(body, /const counts = !!hub\.option\?\.\("counts"\)/);
  assert.match(body, /if \(!counts\) \{ b\.replaceChildren\(document\.createTextNode\(m\[1\]\)\); continue; \}/, "the word alone");
  assert.match(body, /if \(badge\) \{ if \(!counts\)/, "a badge made earlier is taken off when the option goes off");
  assert.match(js, /splitTabs\(\);/, "it runs on every sweep");
});

test("the group count is hidden unless counts are on", () => {
  assert.ok(rules("html.th-clean:not(.th-clean-counts) .gcount").some((b) => /display:\s*none/.test(b)), "hidden without the option");
  assert.ok(rules("html.th-clean .gcount").every((b) => !/display:\s*none/.test(b)), "the plain rule keeps it shown");
  assert.equal(rules("html.th-clean .c-n").length, 1, "the tab badge keeps its style for the ticked option");
  assert.match(js, /const waitCounts = /, "the waiting counter is still drawn");
});
