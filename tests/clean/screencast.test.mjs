// A screen recording in the owner's words (modules/web/static/js/cast.js): the feed shows the video and its length, not
// the app's note to the session about the video and pointer track files; the text the session got is not changed.
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const CAST = pathToFileURL(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "js", "cast.js")).href;
const { castOf, castClock, isCastVideo } = await import(CAST);

const video = { name: "screencast-5db70456.mp4", path: "/files/screencast-5db70456.mp4", size: 2100000, mime: "video/mp4" };
const track = { name: "screencast-5db70456.events.jsonl", path: "/files/screencast-5db70456.events.jsonl", size: 9000 };
const note = (label, clock, rest) => `${label} ${clock}: ${rest} — screencast-5db70456.mp4. ${rest} — screencast-5db70456.events.jsonl.`;
const NOTE_EN = note("Screencast", "1:02", "a recording of my screen with the cursor and my voice; pointer track with timestamps");
const NOTE_DE = note("Bildschirmaufnahme", "0:05", "eine Aufnahme meines Bildschirms mit Zeiger und Stimme; Zeigerspur");
const NOTE_JA = note("画面収録", "0:07", "カーソルと声つきの画面の録画; ポインターの軌跡");

test("the app's note goes in any language: no file names, no track description, the length stays", () => {
  const en = castOf({ text: NOTE_EN, attachments: [video, track] });
  assert.equal(en.text, "");
  assert.equal(en.seconds, 62);
  assert.equal(en.video, video);
  assert.deepEqual(en.files, [], "the track is not a file for the owner");
  const de = castOf({ text: NOTE_DE, attachments: [video, track] });
  assert.equal(de.text, "");
  assert.equal(de.seconds, 5);
  assert.equal(castOf({ text: NOTE_JA, attachments: [video, track] }).seconds, 7);
});

test("an unknown length (\"0:00\") is no length: the page takes it from the video", () => {
  const c = castOf({ text: NOTE_EN.replace("1:02", "0:00"), attachments: [video, track] });
  assert.equal(c.text, "");
  assert.equal(c.seconds, 0);
  assert.equal(c.seconds > 0, false);
});

test("only this video's track is hidden: another pointer track file of the owner stays", () => {
  const other = { name: "run.events.jsonl", path: "/files/run.events.jsonl", size: 100 };
  assert.deepEqual(castOf({ text: NOTE_EN, attachments: [video, track, other] }).files, [other]);
});

test("a note without the track, and the owner's own words around it", () => {
  assert.equal(castOf({ text: NOTE_EN.split(". ")[0] + ".", attachments: [video] }).text, "");
  const c = castOf({ text: "look at the button\n\n" + NOTE_EN, attachments: [video, track] });
  assert.equal(c.text, "look at the button");
});

test("the owner's words that only look like a note stay", () => {
  // the length is there but the line is not about this video's file; the label has a colon before it
  const talk = "Meeting 3:30: see the notes";
  assert.equal(castOf({ text: talk, attachments: [video] }).text, talk);
  const inline = "Re: my screencast 3:30: look at screencast-5db70456.mp4 please";
  assert.equal(castOf({ text: inline, attachments: [video] }).text, inline, "not a paragraph of its own");
});

test("other attachments stay; a message without a screencast is left alone", () => {
  const pic = { name: "shot.png", path: "/files/shot.png", mime: "image/png" };
  assert.deepEqual(castOf({ text: NOTE_EN, attachments: [pic, video, track] }).files, [pic]);
  assert.equal(castOf({ text: "hello", attachments: [pic] }), null);
  assert.equal(castOf({ text: "Screencast", attachments: [] }), null);
  assert.equal(isCastVideo({ name: "movie.mp4" }), false);
  assert.equal(isCastVideo(video), true);
});

test("the length reads as the apps show it", () => {
  assert.equal(castClock(5), "0:05");
  assert.equal(castClock(62.4), "1:02");
  assert.equal(castClock(3723), "1:02:03");
});
