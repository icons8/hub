// A thread with another hub in a session's feed: the words the session sent to a hub through the liaison and the hub's
// answer are one block "Thread with hub X", not a core message with a frame of markers. The page takes everything from
// the fields of the rows (core/views.py: hubbus on an `in` row — whose hub, who wrote there, the words alone; a `bus`
// row — the words the session handed to the liaison for that hub), it does not take the words out of the frame text.
import { el, stamp } from "./dom.js";
import { S, render } from "./state.js";
import { nameOf, paint, whoOf } from "./who.js";
import { t, CLOSED, TO, isolate } from "./i18n.js";

// the name the owner sees for the hub: his own (alias) or what the hub called itself, else the contact's name
const hubName = (contact) => {
  const c = ((S.snap.bus || {}).contacts || []).find((x) => x.name === contact);
  return (c && (c.alias || c.label)) || contact;
};

// the answer takes the nearest earlier request of this feed to the same contact that has no answer yet; a request with no
// answer stays a row of its own. Rows are copied, not changed: the same list is drawn again on every snapshot
export function joinBus(list) {
  if (!list.some((it) => it.kind === "bus")) return list;
  const out = [];
  for (const it of list) {
    if (it.kind === "in" && it.hubbus) {
      let i = out.length - 1;
      while (i >= 0 && !(out[i].kind === "bus" && out[i].contact === it.hubbus.contact)) i--;
      if (i >= 0) { out.push({ ...it, _req: out[i] }); out.splice(i, 1); continue; }
    }
    out.push(it);
  }
  return out;
}

// the words go through the feed's own renderer (`words`, from feed.js): markdown, links, paths and the "more" fold of a long
// message, the same as any message of the feed; there are no attachments on the bus, a link or a text only
const message = (cls, by, node) => {
  const m = el("div", "hubth-m " + cls);
  const txt = el("div", "hubth-txt");
  txt.append(node);
  m.append(by, txt);
  return m;
};

// it: an `in` row with hubbus (the answer, maybe with _req — the request it answers) or a `bus` row (a request alone);
// words(text, id) — the feed's rendering of a message text (feed.js), id keeps the fold of that text between redraws
export function busRow(it, words) {
  const answer = it.kind === "in" ? it : null, req = answer ? answer._req : it;
  const contact = answer ? answer.hubbus.contact : it.contact, name = hubName(contact);
  const key = "b" + (answer ? answer.id : it.id), open = S.openSteps.has(key);
  const r = el("div", "row me from hubth");
  const g = el("span", "g");
  g.setAttribute("aria-hidden", "true");
  const b = el("div", "b");
  const th = el("div", "hubth-thread");
  const head = el("div", "hubth-hd");
  const chip = el("span", "hubth-chip");
  chip.append(el("span", "hubth-arrow", "⇄"), " " + t("web.feed.hub_chip"));   // the sign is its own span: a hook for themes
  head.append(chip, el("b", null, t("web.feed.hub_thread", { name })),
    el("span", "hubth-dim", [answer && answer.hubbus.agent ? answer.hubbus.agent : null,
      t("web.feed.hub_count", { n: (req ? 1 : 0) + (answer ? 1 : 0) }), answer ? null : t("web.feed.hub_waiting")].filter(Boolean).join(" · ")));
  th.append(head);
  if (req) {
    const by = el("div", "hubth-by");
    by.append(el("span", null, `${isolate(S.cur ? nameOf(S.cur.name) : "…")} ${TO} ${isolate(name)} · `), stamp(req.ts));
    th.append(message("out", by, words(req.text, "bo" + req.id)));
  }
  if (answer) {
    const by = el("div", "hubth-by");
    by.append(el("b", null, answer.hubbus.agent ? `${name} · ${answer.hubbus.agent}` : name), el("span", null, " · "), stamp(answer.ts));
    th.append(message("in", by, words(answer.hubbus.body, "bi" + answer.id)));
    // what the session itself got (the frame, the markers) — for the one who wants to see it, folded
    const svc = el("div", "hubth-svc");
    const btn = el("button", "linkbtn", `${open ? "▾" : CLOSED} ${t("web.feed.hub_svc")}`);
    btn.type = "button";
    btn.setAttribute("aria-expanded", String(open));
    btn.addEventListener("click", () => { open ? S.openSteps.delete(key) : S.openSteps.add(key); render(); });
    svc.append(btn);
    if (open) svc.append(el("pre", null, answer.text));
    th.append(svc);
  }
  b.append(th);
  r.append(g, b);
  return paint(r, whoOf(contact));
}
