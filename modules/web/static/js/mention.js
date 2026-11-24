// Board cards named in a message to a direction session. The owner picks cards on the board (js/boards.js); the picks
// wait above the session's input field as a row of chips ("About tasks: TASK-2 ✕ TASK-5 ✕") and go with the next message:
// as the `tasks` field of /api/send (the core puts one line with the cards into the session's label and the feed shows
// the chips under the words), or, when the server does not announce `send.tasks` in the snapshot's features (the
// previous release's server runs for a few minutes while the new page files are already on disk), as a line in front of
// the text. The picks are kept per direction in the browser: a reload, board ↔ feed and a failed send keep them.
import { el, icon } from "./dom.js";
import { banner } from "./api.js";
import { boxes } from "./boxes.js";
import { S } from "./state.js";
import { t } from "./i18n.js";

const KEY = "hub.taskPicks";
const MAX = 40;   // the core takes this many cards in one message (core/board.py TASKS_MAX)
const picks = new Map();   // direction -> [{id, title, status}] in the order picked
try {
  for (const [slug, list] of Object.entries(JSON.parse(localStorage.getItem(KEY) || "{}")))
    if (Array.isArray(list)) picks.set(slug, list.filter((x) => x && typeof x.id === "string").slice(0, MAX)
      .map((x) => ({ id: x.id, title: String(x.title ?? ""), status: String(x.status ?? "") })));
} catch { /* no storage, or not ours — start empty */ }

function save() {
  try {
    const all = Object.fromEntries([...picks].filter(([, l]) => l.length));
    if (Object.keys(all).length) localStorage.setItem(KEY, JSON.stringify(all)); else localStorage.removeItem(KEY);
  } catch { /* only until reload */ }
}

export const list = (slug) => picks.get(slug) || [];
export const ids = (slug) => list(slug).map((x) => x.id);
export const has = (slug, id) => list(slug).some((x) => x.id === id);
export const limit = MAX;

const trays = new Map();   // direction -> the chip row's node: the composer keeps it between snapshots

function changed(slug) {
  save();
  drawTray(slug);
  boxes.get(slug)?.refresh();   // the row appears in the field or goes away
}
const card = (x) => ({ id: x.id, title: String(x.title ?? ""), status: String(x.status ?? "") });
export function set(slug, items) {
  const seen = new Set(), all = items.filter((x) => !seen.has(x.id) && seen.add(x.id));
  picks.set(slug, all.slice(0, MAX).map(card));
  changed(slug);
  if (all.length > MAX) banner(t("web.mention.limit", { count: MAX }));   // the rest was not taken: say so, not silently
}
export const toggle = (slug, x) => set(slug, has(slug, x.id) ? list(slug).filter((y) => y.id !== x.id) : [...list(slug), x]);
export const clear = (slug) => set(slug, []);
// a send failed: the cards go back, ahead of what was picked since
export const restore = (slug, items) => set(slug, [...items, ...list(slug)]);

// a fresh board of the direction: titles and statuses are taken from it (a card moved to another column keeps its pick),
// a card that is no longer on the board (archived) drops out
export function known(slug, tasks) {
  const now = new Map(tasks.map((x) => [x.id, x]));
  const was = list(slug);
  const next = was.filter((x) => now.has(x.id)).map((x) => ({ id: x.id, title: now.get(x.id).title, status: now.get(x.id).status }));
  if (JSON.stringify(next) !== JSON.stringify(was)) set(slug, next);
}

const short = (s, n = 38) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// the chip row above the field; one node per direction
export function tray(slug) {
  if (!trays.has(slug)) { trays.set(slug, el("div", "pending mention-bar")); drawTray(slug); }
  return trays.get(slug);
}
function drawTray(slug) {
  const n = trays.get(slug);
  if (!n) return;
  const items = list(slug);
  n.replaceChildren(...(items.length ? [el("span", "mention-lead", t(items.length > 1 ? "web.mention.lead_many" : "web.mention.lead_one"))] : []),
    ...items.map((x) => {
      const c = el("span", "pchip mchip");
      c.title = [x.id, x.title, x.status && `(${x.status})`].filter(Boolean).join(" ");
      c.append(el("b", null, x.id), ...(x.title ? [el("span", "nm", short(x.title))] : []));
      const b = el("button", "x");
      b.type = "button";
      b.append(icon("close"));
      b.setAttribute("aria-label", t("web.mention.remove", { id: x.id }));
      b.addEventListener("click", () => toggle(slug, x));
      c.append(b);
      return c;
    }));
}

// what a message to the direction carries: {tasks} for a server that knows the field, otherwise the text with a line in
// front of the owner's words. No picks — nothing. items — the picks taken earlier (a voice message: when "send" was pressed)
export function carry(slug, text, items = list(slug)) {
  if (!items.length) return {};
  if ((S.snap?.features || []).includes("send.tasks")) return { tasks: items.map(card) };
  const line = items.map((x) => `${x.id}${x.title ? ` «${short(x.title, 60)}»` : ""}${x.status ? ` (${x.status})` : ""}`).join("; ");
  return { text: `${t("web.mention.prefix", { list: line })}\n\n${text}` };
}

// "TASK-2 Title" per line — for the clipboard
export const listText = (slug) => list(slug).map((x) => (x.title ? `${x.id} ${x.title}` : x.id)).join("\n");

// the chips under the owner's words in the feed
export function chipRow(items) {
  const row = el("div", "mention-bar sent");
  row.append(el("span", "mention-lead", t(items.length > 1 ? "web.mention.lead_many" : "web.mention.lead_one")),
    ...items.map((x) => {
      const c = el("span", "pchip mchip");
      c.title = [x.id, x.title, x.status && `(${x.status})`].filter(Boolean).join(" ");
      c.append(el("b", null, x.id), ...(x.title ? [el("span", "nm", short(x.title))] : []));
      return c;
    }));
  return row;
}
