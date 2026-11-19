// The braces of Clean's style sheets balance. A section glued on in a merge that lost one closing brace leaves everything
// below it inside the last @media block: the page then looks fine in a browser that matches the query, and the rules
// after it do not apply anywhere else — no other check notices.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");

// the depth of braces at the start of every "/* ---- section" comment and at the end, comments and strings left out
const depths = (text) => {
  const at = [];
  let depth = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (text.startsWith("/*", i)) {
      if (text.startsWith("/* ----", i)) at.push([depth, text.slice(0, i).split("\n").length]);
      i = text.indexOf("*/", i) + 1;
    } else if (c === '"' || c === "'") {
      for (i++; text[i] !== c && i < text.length; i++) if (text[i] === "\\") i++;
    } else if (c === "{") depth++;
    else if (c === "}") depth--;
  }
  return { at, end: depth };
};

for (const file of ["clean.css", "palette.css"]) {
  test(`${file}: every section starts outside a block, the braces balance`, () => {
    const { at, end } = depths(fs.readFileSync(path.join(DIR, file), "utf8"));
    assert.deepEqual(at.filter(([depth]) => depth !== 0), [], "a section starts inside an unclosed block ([depth, line])");
    assert.equal(end, 0, "unclosed blocks at the end of the file");
  });
}
