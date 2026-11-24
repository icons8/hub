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

// The notes as the native apps write them (hub-ios L10n cast.note, cast.note.plain, cast.note.track), word for word.
// A note without the length: the app writes it when it does not know the length of the recording.
const NAME = "screencast-3305d147";
const cast = { name: `${NAME}.mp4`, path: `/files/${NAME}.mp4`, mime: "video/mp4" };
const castTrack = { name: `${NAME}.events.jsonl`, path: `/files/${NAME}.events.jsonl` };
const TRACK_EN = ` Pointer track with timestamps (moves, clicks, scrolling; t — seconds from the first frame, x and y — video pixels) — ${NAME}.events.jsonl.`;
const SENT_EN = `Screencast 0:04: a recording of my screen with the cursor and my voice — ${NAME}.mp4.` + TRACK_EN;
const PLAIN_EN = `Screencast: a recording of my screen with the cursor and my voice — ${NAME}.mp4.` + TRACK_EN;
const PLAIN_ES = `Screencast: grabación de mi pantalla con el cursor y mi voz — ${NAME}.mp4. Pista del puntero con marcas de tiempo `
  + `(movimientos, clics, desplazamiento; t — segundos desde el primer fotograma, x e y — píxeles del vídeo) — ${NAME}.events.jsonl.`;
const PLAIN_DE = `Bildschirmaufnahme: eine Aufnahme meines Bildschirms mit Zeiger und Stimme — ${NAME}.mp4.`;
const shown = (text, attachments = [cast, castTrack]) => castOf({ text, attachments });

test("a note without the length goes too: no file names in the bubble, the length is not known", () => {
  for (const note of [PLAIN_EN, PLAIN_ES, PLAIN_DE, PLAIN_EN.replace(/\.$/, "")]) {
    const c = shown(note);
    assert.equal(c.text, "", note);
    assert.equal(c.seconds, null, "no length in the note: the page takes it from the video");
    assert.deepEqual(c.files, []);
  }
  assert.equal(shown(SENT_EN).text, "");
  assert.equal(shown(SENT_EN).seconds, 4);
});

test("the owner's words before the note on the same line: the dash goes with the note", () => {
  for (const [note, seconds] of [[SENT_EN, 4], [PLAIN_EN, null], [PLAIN_ES, null]]) {
    const c = shown("look at the button — " + note);
    assert.equal(c.text, "look at the button", "no dash left hanging");
    assert.equal(c.seconds, seconds);
    assert.equal(shown("look at the button\n\n" + note).text, "look at the button");
    assert.equal(shown(note + "\n\nand this one too").text, "and this one too");
  }
  // the owner's line has a label and a colon of its own: only the note goes
  const same = shown(`Screencast: look at save — ${SENT_EN}`);
  assert.equal(same.text, "Screencast: look at save");
  assert.equal(same.seconds, 4);
  assert.equal(shown(`Look: the button — ${PLAIN_EN}`).text, "Look: the button");
  // words after the note on the same line stay too
  assert.equal(shown(`${PLAIN_EN} and the save too`).text, "and the save too");
});

test("the owner's words glued to the note without the dash are not lost", () => {
  // no " — " between them: where the owner's words end is not known, so the note stays whole rather than eat them
  for (const own of [`look: ${PLAIN_EN}`, `Note: see ${SENT_EN}`]) assert.equal(shown(own).text, own, own);
});

test("the owner's line in the note's own shape, with this video's name, goes as the note: on purpose", () => {
  // "<word>: <text> — <this video's name>.mp4" is the note's shape, and the label is not a list of languages. The name is
  // random and only the app's note carries it, so such a line is a copy of the note; the session still gets it as typed.
  for (const own of [`Bug: crash — ${NAME}.mp4`, `look at the button — Bug: crash — ${NAME}.mp4.`]) {
    const c = shown(own, [cast]);
    assert.equal(c.text, own.startsWith("look") ? "look at the button" : "", own);
    assert.equal(c.seconds, null);
  }
});

test("the owner's words with this video's name, not in the note's shape, stay", () => {
  for (const own of [
    `Screencast: look at ${NAME}.mp4`,                        // no dash before the name
    `Screencast: a bug here - ${NAME}.mp4`,                   // a hyphen, not the note's dash
    `Screencast: look\n— ${NAME}.mp4`,                        // the dash on a line of its own
    `look at the button — ${NAME}.mp4`,                       // no label before the name
    `look\n\nScreencast: ${NAME}.mp4 shows the save error.`,  // the module's summary naming the file
  ]) assert.equal(shown(own).text, own, own);
  const other = "Screencast: a recording of my screen with the cursor and my voice — screencast-aaaaaaaa.mp4.";
  assert.equal(shown(other).text, other, "a note about another recording is not touched");
});

test("the length reads as the apps show it", () => {
  assert.equal(castClock(5), "0:05");
  assert.equal(castClock(62.4), "1:02");
  assert.equal(castClock(3723), "1:02:03");
});

test("a note with the length may have a colon of its own in its text: it goes whole, the length is read", () => {
  // the length anchors the shape, so a ": " in the note's text does not keep it in the bubble with the file names
  const name = "screencast-ab12", video = { name: `${name}.mp4`, path: `/files/${name}.mp4`, mime: "video/mp4" };
  const note = `Screencast 0:04: a recording of my screen (note: voice) — ${name}.mp4.`;
  for (const [text, own] of [[note, ""], [`look at the button — ${note}`, "look at the button"], [`${note}\n\nand this`, "and this"]]) {
    const c = castOf({ text, attachments: [video] });
    assert.equal(c.text, own, text);
    assert.equal(c.seconds, 4);
  }
});

test("without the length the label is one word: the owner's word before it on the line is not eaten", () => {
  // "look Screencast: …" — two words before the colon and no length: where the owner's words end is not known, so the
  // line stays whole, as the main rule kept it, rather than lose "look"
  const name = "screencast-ab12", video = { name: `${name}.mp4`, path: `/files/${name}.mp4`, mime: "video/mp4" };
  for (const own of [`look Screencast: a recording — ${name}.mp4.`, `look at Screencast: a recording — ${name}.mp4.`]) {
    const c = castOf({ text: own, attachments: [video] });
    assert.equal(c.text, own, own);
  }
});
