// A screencast in the owner's words: the screen recording with the cursor and the voice (the iPhone and Mac apps record
// it). The message carries the video `screencast-<id>.mp4` and its pointer track `screencast-<id>.events.jsonl`; the app
// also writes the session a note about both files ("Screencast 0:05: a recording of my screen … — screencast-<id>.mp4.
// Pointer track … — screencast-<id>.events.jsonl."). That note and the file names are for the session: the feed shows the
// video and "Screen recording · 0:05" instead, the text the session got stays as is.
// The same rule as the native apps (hub-ios PublicFeed+Cast, ChatView castName). No imports: the checks load it in Node.
// The note's label is whatever the app wrote it in (a word or three, then the length): the note is found by its shape
// and by the names of this very video, not by a list of languages.

export const isCastVideo = (f) => /^screencast-[\w-]+\.mp4$/i.test(String(f?.name || ""));
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// "0:05" → 5, "1:02:03" → 3723
const seconds = (clock) => clock.split(":").reduce((n, p) => n * 60 + Number(p), 0);

// The video, the owner's own words and what is known of the recording, or null when the message has no screencast.
// text — the words without the app's note; "" when there was only the note. files — the other attachments (the track goes
// away with the video). seconds — the length from the note, null when it is not known.
export function castOf(it) {
  const all = it?.attachments || [];
  const video = all.find(isCastVideo);   // only the first: the apps send one screencast per message
  if (!video) return null;
  const name = String(video.name).replace(/\.mp4$/i, "");
  const track = `${name}.events.jsonl`.toLowerCase();   // only this video's track goes; another .events.jsonl of the owner stays
  const files = all.filter((f) => f !== video && String(f?.name || "").toLowerCase() !== track);
  let text = String(it.text || "").trim(), length = null;
  // a paragraph of its own: the label (1–3 words without digits or colons), the length, a colon, and this video's file name
  const note = new RegExp(`(?<![^\\n])(?:[^\\s\\d:]+ ){1,3}(\\d+(?::\\d{2}){1,2}): [^\\n]*?${escape(name)}\\.mp4\\.?`
    + `(?:[^\\n]*?${escape(name)}\\.events\\.jsonl\\.?)?`, "i");
  const m = text.match(note);
  if (m) {
    length = seconds(m[1]);
    text = (text.slice(0, m.index) + text.slice(m.index + m[0].length)).replace(/\n{3,}/g, "\n\n").trim();
  }
  return { video, files, text, seconds: length };
}

// 5 → "0:05", 3723 → "1:02:03" — as the voice recorder and the native apps show a length
export function castClock(s) {
  const n = Math.max(0, Math.round(s));
  const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), sec = String(n % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}
