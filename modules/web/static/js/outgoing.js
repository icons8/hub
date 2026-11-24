// The owner's voice message before the core has it: it is in the feed of its session as soon as "send" is pressed —
// "Recognizing…" in the place of its words, then the words, then the core's own row in the same place. The bubble is tied
// to the send by its client_id (the field sends with it, API 1.28) and leaves in the same redraw that brings the core's
// row (by the seq from the receipt, or by its words): no second copy and no blink. A failed or cancelled send takes it
// away at once (the reason and "Retry" are under the field, as for any send); an accepted one whose row the feed never
// shows is dropped after SEEN_MS, in any session.
import { newClientId } from "./api.js";
import { S, msgState, renderLive } from "./state.js";

const SEEN_MS = 30000;
const held = new Map();   // client_id -> {cid, session, ts, text, seq, at (accepted at), after, claimed}

// the feed's last row (without spreading a long timeline into arguments)
const lastSeq = (items) => items.reduce((m, it) => Math.max(m, it.seq || 0), 0);

// a bubble for a message to session that is still being made (a recording being transcribed); returns its client_id
export function hold(session) {
  const cid = newClientId();
  const after = lastSeq(S.snap?.timelines?.[session] || []);   // its row comes after the feed's last one of this moment
  held.set(cid, { cid, session, ts: Date.now() / 1000, text: null, seq: null, at: 0, after, claimed: false });
  renderLive();
  return cid;
}

// the words are known (the transcription is ready): the bubble shows them while the send is on its way
export function fill(cid, text) {
  const h = held.get(cid);
  if (!h) return;
  h.text = text;
  renderLive();
}

// the field took the message and sends it under this client_id (composer.js), with these words (the field's text of the
// press may follow the spoken ones)
export function claim(cid, text) {
  const h = held.get(cid);
  if (!h) return;
  h.claimed = true;
  if (text != null) { h.text = text; renderLive(); }
}

// the words went to the send: a bubble no send took (nothing was sent, or the words wait in the field for a file still
// uploading — they are visible there) goes at once instead of waiting for a row that will not come
export function release(cid) {
  if (held.get(cid)?.claimed === false) settled(cid);
}

// the core accepted the send (seq — its event, when the receipt names it): the bubble waits for its row, matched by that
// seq or by its words, for SEEN_MS at most. Client ids without a bubble (an ordinary typed send) are ignored
export function sent(cid, seq) {
  const h = held.get(cid);
  if (!h) return;
  if (Number.isFinite(seq)) h.seq = seq;
  h.at = Date.now();
  renderLive();
  setTimeout(renderLive, SEEN_MS + 50);   // a quiet hub redraws nothing by itself: the bubble whose row never came goes on time
}

// nothing will come of it — refused, unknown, stuck, cancelled or no speech: the bubble goes
export function settled(cid) {
  if (held.delete(cid)) renderLive();
}

// the core's row of this message: by the seq from the receipt, or — the stream may bring the row before the send's own
// answer — the same words, said after the bubble appeared
const mine = (h, it) => it.kind === "in" && (h.seq != null ? it.seq === h.seq
  : h.text != null && it.seq > h.after && String(it.text || "").trim() === h.text.trim());

// the core has the message and is handing it to the session ("Sending"): its row is not in the feed yet (main.js
// feedList, state.js notDelivered) — the bubble keeps its place until the row takes it, and the strip above the field does
// not repeat it. A message queued for a busy session or put aside is the strip's: there it can be removed or sent at once
const onItsWay = (it) => msgState(it) === "typing";

// the bubbles to draw at the end of session's feed (items — its live rows, delivered or not): one whose row is already
// there is gone in this very redraw; an accepted one whose row never came is gone after SEEN_MS, whichever feed is open
export function waiting(session, items) {
  const out = [];
  for (const h of [...held.values()]) {
    if (h.at && Date.now() - h.at > SEEN_MS) { held.delete(h.cid); continue; }
    if (h.session !== session) continue;
    const row = items.find((it) => mine(h, it));
    if (row && !onItsWay(row)) { held.delete(h.cid); continue; }
    out.push({ kind: "outgoing", seq: 0, id: h.cid, ts: h.ts, text: h.text });
  }
  return out;
}

// this row of session's timeline is shown by its bubble (strip.js leaves its "Sending" line out); asked before or after
// the feed is drawn — it does not rely on that order
export const inBubble = (it, session) => onItsWay(it) && [...held.values()].some((h) => h.session === session && mine(h, it));
