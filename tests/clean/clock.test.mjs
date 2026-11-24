// The page's one clock (js/clock.js): a 12-hour locale writes the hour without a leading zero ("1:09 AM", not "01:09 AM"),
// a 24-hour one keeps it ("01:09"), as the native app shows the time; every time on the page — the feed, the Clean theme,
// the settings lists, the bundled views (hub.clock) — goes through it, so no file builds its own hour format. ICU's exact
// text differs between builds (the space before AM, "a. m." or "a.m."), so the checks look at the hour and the minutes.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { clock } from "../../modules/web/static/js/clock.js";

const web = path.join(import.meta.dirname, "..", "..", "modules", "web");
// the page's own scripts and the bundled views' ones, as [name under modules/web, full path]
const files = () => [path.join(web, "static", "js"), path.join(web, "static", "clean"),
  ...fs.readdirSync(path.join(web, "bundled")).map((d) => path.join(web, "bundled", d))]
  .filter((d) => fs.statSync(d).isDirectory())
  .flatMap((d) => fs.readdirSync(d).filter((n) => n.endsWith(".js")).map((n) => [path.relative(web, path.join(d, n)), path.join(d, n)]));
const at = (h, m) => new Date(2026, 10, 17, h, m).getTime() / 1000;   // local time: the same text in any time zone

test("a 12-hour locale writes the hour without a leading zero", () => {
  assert.match(clock(at(1, 9), "en-US"), /^1:09\s?AM$/u);
  assert.match(clock(at(13, 9), "en-US"), /^1:09\s?PM$/u);
  assert.match(clock(at(1, 9), "hi"), /(^|\D)1:09(\D|$)/u);
});

test("a 24-hour locale keeps the two-digit hour, minutes are always two digits", () => {
  assert.equal(clock(at(1, 9), "ru"), "01:09");
  assert.equal(clock(at(13, 5), "de"), "13:05");
  assert.match(clock(at(1, 9), "es"), /^01:09$/u);   // the hub passes plain "es", a 24-hour locale
});

test("a date before the time uses the same hour", () => {
  assert.match(clock(at(1, 9), "en-US", { day: "numeric", month: "short" }), /^Nov 17,? 1:09\s?AM$/u);
  assert.match(clock(at(1, 9), "de", { day: "numeric", month: "short" }), /^17\. Nov\.?,? 01:09$/u);
});

test("a time given as an ISO string (the secrets vault keeps those) is the same time", () => {
  const iso = new Date(2026, 10, 17, 1, 9).toISOString();
  assert.equal(clock(iso, "ru"), "01:09");
  const date = { day: "numeric", month: "short" };
  assert.equal(clock(iso, "en-US", date), clock(at(1, 9), "en-US", date));
});

test("a time that is not a time writes nothing, and a number in a string is seconds", () => {
  for (const ts of [undefined, null, NaN, "", "soon", {}]) assert.equal(clock(ts, "ru"), "", String(ts));   // not RangeError: one row must not break the list
  assert.equal(clock(String(at(1, 9)), "ru"), "01:09");   // the old ts * 1000 took a number in a string
});

test("the clock follows the computer to another time zone, as the feed's dates do (a minute at most)", (t) => {
  const zone = process.env.TZ, now = 1000000000000;   // the fixture's "now": the clock's formatters live a minute of it
  t.after(() => { if (zone === undefined) delete process.env.TZ; else process.env.TZ = zone; });
  t.mock.method(Date, "now", () => now);
  const ts = Date.UTC(2026, 10, 17, 1, 9) / 1000;
  process.env.TZ = "UTC";
  assert.equal(clock(ts, "ru"), "01:09");
  process.env.TZ = "Asia/Tokyo";
  assert.equal(clock(ts, "ru"), "01:09");   // within the minute — the formatter made in UTC
  Date.now.mock.mockImplementation(() => now + 60001);
  assert.equal(clock(ts, "ru"), "10:09");   // a minute later — made again, in the new zone
});

test("no file of the page or of a bundled view builds its own hour format", () => {
  // a bundled view's fallback for a page not reloaded after an update (no hub.clock) keeps the view's old format: the line
  // that asks hub.clock, or the one right after it
  const own = files().filter(([n, f]) => !n.endsWith("clock.js") && fs.readFileSync(f, "utf8").split("\n")
    .some((line, i, all) => /\bhour:\s*["'`]/.test(line) && !/\bhub\.clock\?\.\(/.test(line + (all[i - 1] || ""))));
  assert.deepEqual(own.map(([n]) => n), [], "these files format the hour themselves instead of through js/clock.js");
});

test("every file that takes the clock still parses (a local clock() of its own would clash with the import)", () => {
  const broken = files().filter(([, f]) => {
    const src = fs.readFileSync(f, "utf8");
    return src.includes("clock.js") && spawnSync(process.execPath, ["--input-type=module", "--check"], { input: src }).status !== 0;
  });
  assert.deepEqual(broken.map(([n]) => n), [], "these files do not parse");
});
