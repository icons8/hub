// Icons of the list tools of Clean: every action has its own drawing (no two actions share one icon),
// the files named in clean.css exist, and the list in icons8.json matches them. Browserless.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const read = (f) => fs.readFileSync(path.join(root, f), "utf8");
const css = read("clean.css");
const items = JSON.parse(read("icons8.json")).icons.items;

test("sort and reset in the group tools are not the queue's icon and not the reply's undo", () => {
  assert.match(css, /\.g-tools \.auto \{ --g-ic: url\("sort\.svg"\); \}/);
  assert.match(css, /\.g-tools \.btn:not\(\.add-group, \.auto\) \{ --g-ic: url\("reset\.svg"\); \}/);
  assert.ok(!fs.existsSync(path.join(root, "undo.svg")));
  assert.equal(items["auto-sort"].commonName, "sorting-arrows");
  assert.equal(items["reset-groups"].commonName, "replay");
  assert.notEqual(items["auto-sort"].id, items.queue.id);
  assert.notEqual(items["reset-groups"].id, items.reply.id);
});

test("the drawings themselves differ: sort is not the queue's, reset is not the reply's", () => {
  const d = (f) => read(f).match(/ d="([^"]+)"/)[1];
  assert.notEqual(d("sort.svg"), d("queue.svg"));
  assert.notEqual(d("reset.svg"), d("reply.svg"));
  assert.notEqual(d("sort.svg"), d("reset.svg"));
});

test("every svg named in clean.css exists", () => {
  for (const m of css.matchAll(/url\("([\w-]+\.svg)"\)/g)) assert.ok(fs.existsSync(path.join(root, m[1])), m[1]);
});

test("\"Send now\" in the queue is an arrow into a frame, not the replay's U-turn", () => {
  assert.match(css, /\.btn\.clean-now \{ --clean-i: url\("now\.svg"\);/);
  assert.ok(!fs.existsSync(path.join(root, "steer.svg")));
  assert.equal(items.now.commonName, "enter-2");
  assert.equal(items.steer, undefined);
  for (const other of ["reply", "reset-groups", "queue"]) assert.notEqual(items.now.id, items[other].id);
  const d = (f) => read(f).match(/ d="([^"]+)"/)[1];
  for (const f of ["reply.svg", "reset.svg", "queue.svg"]) assert.notEqual(d("now.svg"), d(f));
});
