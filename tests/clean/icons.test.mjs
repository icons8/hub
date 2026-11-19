// Icons of Clean: every icon drawn from Icons8 is listed in icons8.json with its id, so where a drawing comes from reads
// from the list; the account icon is the Forma Regular "user", not a drawing made by hand. Browserless: the files are read
// as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const read = (f) => fs.readFileSync(path.join(root, f), "utf8");
const items = JSON.parse(read("icons8.json")).icons.items;

test("the account, failed and compass icons are listed with their Icons8 ids", () => {
  assert.deepEqual(items.account, { id: "Or3Eb98tGbJL", commonName: "user" });
  assert.deepEqual(items.failed, { id: "Qgjnp78cDeJi", commonName: "delete-sign" });
  assert.deepEqual(items.compass, { id: "agP5MiwyBE5K", commonName: "compass" });
});

test("account.svg is the Forma drawing: a rounded head over the shoulders, no hand-set fill", () => {
  const svg = read("account.svg");
  assert.doesNotMatch(svg, /fill="/);
  assert.match(svg, /^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" viewBox="0 0 24 24"><path d="M 12 2 C /);
});
