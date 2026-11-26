// The core's banner is the first row of the chat card: its top corners are the card's. Beside the side panel the card's end corners
// are square (the card and the panel are one window), so the banner's top end corner is square there too. Reads clean.css as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8").replace(/\r\n/g, "\n");

test("the banner follows the card: round 16 alone, square at the joint with the side panel", () => {
  assert.match(css, /html\.th-clean main > \.banner\.on \{[^}]*border-radius: 16px 16px 0 0/);
  const media = css.slice(css.indexOf("@media (min-width: 761px) {\n  html.th-clean .app.with-side > main"));
  const side = media.slice(0, media.indexOf("\n}\n"));
  assert.match(side, /\.app\.with-side > main \{ border-start-end-radius: 0; border-end-end-radius: 0; \}/);
  assert.match(side, /\.app\.with-side > main > \.banner\.on \{ border-start-end-radius: 0; \}/);
});
