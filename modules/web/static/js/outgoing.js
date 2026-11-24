// The owner's message before the core has it, in the feed of its session as soon as "send" is pressed: a voice one —
// "Recognizing…" in the place of its words, then the words, then the core's own row in the same place; a typed one — on
// Enter, "sending", when the core's row will say the same words (fits). The bubble is tied
// to the send by its client_id (the field sends with it, API 1.28) and leaves in the same redraw that brings the core's
// row (by the seq from the receipt, or by its words): no second copy and no blink. A failed or cancelled send takes it
// away at once (the reason and "Retry" are under the field, as for any send); an accepted one whose row the feed never
// shows is dropped after SEEN_MS, in any session.
import { newClientId } from "./api.js";
import { S, msgState, renderLive } from "./state.js";
import { memberOf } from "./who.js";

const SEEN_MS = 30000;
// client_id -> {cid, session, ts, text, quote, tasks, files, seq, at (accepted at), after, claimed, order, left (sent at)}
const held = new Map();
let ver = 0;   // bumped on every change of held: inBubble's answer for a redraw is reused until it changes
let sends = 0;   // the order the sends left in: a row of the same words goes to the bubble whose send left first

// the feed's last row (without spreading a long timeline into arguments)
const lastSeq = (items) => items.reduce((m, it) => Math.max(m, it.seq || 0), 0);

// one record shape for a voice and a typed bubble; its row comes after the feed's last one of this moment
function keep(cid, session, rec) {
  held.set(cid, { cid, session, ts: Date.now() / 1000, text: null, quote: null, tasks: null, files: [], seq: null, at: 0,
    after: lastSeq(S.snap?.timelines?.[session] || []), claimed: false, order: Infinity, left: 0, ...rec });
  ver++;
  renderLive();
  return cid;
}

// a bubble for a message to session that is still being made (a recording being transcribed); returns its client_id
export const hold = (session) => keep(newClientId(), session, {});

// The core's row of a typed message will say what the field sent, in the next moments: only then a bubble is drawn.
// Not for a command ("/name": the core keeps it "typing" until its result, the strip says "Running"), files without
// words (the row says "(attachments)"), a session the core will not hand it to at once — busy, starting, held, asleep,
// parked (no process: idle in the snapshot, with parked) or compacting; the message waits in the queue, the strip's
// "In the queue" —, one with an open card or permission (the words may become its answer: "Answer: …")
export function fits(body) {
  const s = (S.snap?.sessions || []).find((x) => x.name === body.session);
  const text = String(body.text || "").trim();
  return !!s && !!text && !text.startsWith("/") && s.state === "idle" && !s.parked && !s.compacting && !(s.pending || []).length;
}

// a typed message the field sends under client_id cid (body — what goes to the core; files — the field's attachments,
// a picture with the preview it had there): its bubble is in the feed at once, before the core has it, drawn as the
// core's row will be. A repeat of a send under the same id keeps the one bubble
export function typed(cid, body, files = []) {
  if (!cid || held.has(cid) || !fits(body)) return;
  keep(cid, body.session, { text: body.text, quote: body.quote || null, tasks: body.tasks || null, claimed: true, order: ++sends, left: Date.now(),
    files: files.filter((p) => p.meta).map((p) => ({ ...p.meta, preview: p.preview || null })) });
}

// the words are known (the transcription is ready): the bubble shows them while the send is on its way
export function fill(cid, text) {
  const h = held.get(cid);
  if (!h) return;
  h.text = text;
  ver++;
  renderLive();
}

// the field took the message and sends it under this client_id (composer.js), with these words (the field's text of the
// press may follow the spoken ones)
export function claim(cid, text) {
  const h = held.get(cid);
  if (!h) return;
  h.claimed = true;
  h.order = ++sends;
  h.left = Date.now();
  ver++;
  if (text != null) h.text = text;
  renderLive();
}

// the words went to the send: a bubble no send took (nothing was sent, or the words wait in the field for a file still
// uploading — they are visible there) goes at once instead of waiting for a row that will not come
export function release(cid) {
  if (held.get(cid)?.claimed === false) settled(cid);
}

// the core accepted the send (seq — its event, when the receipt names it): the bubble waits for its row, matched by that
// seq or by its words, for SEEN_MS at most. Client ids without a bubble (a send that got none, see fits) are ignored
export function sent(cid, seq) {
  const h = held.get(cid);
  if (!h) return;
  if (Number.isFinite(seq)) h.seq = seq;
  h.at = Date.now();
  ver++;
  renderLive();
  setTimeout(renderLive, SEEN_MS + 50);   // a quiet hub redraws nothing by itself: the bubble whose row never came goes on time
}

// nothing will come of it — refused, unknown, stuck, cancelled or no speech: the bubble goes
export function settled(cid) {
  if (held.delete(cid)) { ver++; renderLive(); }
}

// the core's row of this message: by the seq from the receipt, or — the stream may bring the row before the send's own
// answer — the same words, said after the bubble appeared, as the owner's own words to this session (feed.js messageOf: not a
// session's, a plugin's, another hub's or a group member's row, nor the copy of his words to another session)
const mine = (h, it) => it.kind === "in" && (h.seq != null ? it.seq === h.seq
  : h.text != null && it.seq > h.after && !it.sender && !it.to && !it.agent && !it.hubbus && memberOf(it) === null
    && String(it.text || "").trim() === h.text.trim());

// the core has the message and is handing it to the session ("Sending"): its row is not in the feed yet (main.js
// feedList, state.js notDelivered) — the bubble keeps its place until the row takes it, and the strip above the field does
// not repeat it. A message queued for a busy session or put aside is the strip's: there it can be removed or sent at once
const onItsWay = (it) => msgState(it) === "typing";

// an accepted bubble whose row never came is gone after SEEN_MS, whichever feed is open
function sweep() {
  for (const h of [...held.values()]) if (h.at && Date.now() - h.at > SEEN_MS) { held.delete(h.cid); ver++; }
}

// each bubble of session with its row (or null): one row is one bubble's — two quick sends of the same words are two
// bubbles, and the first row takes only the first of them. A row named by a receipt is that bubble's alone; the rest
// match by words, in the order their sends left (a voice message started earlier but sent later waits for its own)
function rows(session, items) {
  const own = [...held.values()].filter((h) => h.session === session).sort((a, b) => a.order - b.order);
  const named = new Set(own.map((h) => h.seq).filter((x) => x != null));
  const taken = new Set();
  return own.map((h) => {
    const row = h.seq != null ? items.find((it) => mine(h, it))
      : items.find((it) => mine(h, it) && !named.has(it.seq) && !taken.has(it.seq));
    if (row) taken.add(row.seq);
    return [h, row || null];
  });
}

// the bubbles to draw at the end of session's feed (items — its live rows, delivered or not): one whose row is already
// there is gone in this very redraw
export function waiting(session, items) {
  sweep();
  const out = [];
  for (const [h, row] of rows(session, items)) {
    if (row && !onItsWay(row)) { held.delete(h.cid); ver++; continue; }
    out.push({ kind: "outgoing", seq: 0, id: h.cid, ts: h.ts, left: h.left, text: h.text, quote: h.quote, tasks: h.tasks, files: h.files });
  }
  return out;
}

// this row of session's timeline is shown by its bubble (strip.js leaves its "Sending" line out); asked before or after
// the feed is drawn — it does not rely on that order. Without a bubble of the session (almost always) it costs nothing;
// with one, the rows are matched once per change of the bubbles or of the timeline, not once per strip line
let memo = { ver: -1, session: null, items: null, n: -1, taken: null };
export function inBubble(it, session) {
  if (!onItsWay(it)) return false;
  sweep();
  if (![...held.values()].some((h) => h.session === session)) return false;
  const items = S.snap?.timelines?.[session] || [];
  if (!items.includes(it)) return rows(session, [...items, it]).some(([, row]) => row === it);   // not in the timeline yet: rare
  if (memo.ver !== ver || memo.session !== session || memo.items !== items || memo.n !== items.length)
    memo = { ver, session, items, n: items.length, taken: new Set(rows(session, items).map(([, row]) => row)) };
  return memo.taken.has(it);
}
