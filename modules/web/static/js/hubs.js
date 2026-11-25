// The hub bus on the page (spec hubbus/ux §3.2–3.4, §3.6, §7, §8, §10): Settings → Hubs — «Make an invitation» (A: the
// first time one consent to the keys, the library and the outside relay, then the invitation — the core's link — and its
// steps), «Accept an invitation» (B: a preview, then «Accept»; the relay operator's invitation goes into the same
// field, and right after it «Make an invitation» is offered), the linked hubs with their
// pair code and «Matches», whose hub it is (a person of the contacts book), «Unlink»; the «Hubs» group of the session list
// once a hub is linked; a contact's page — the owners' chat (no liaison, no card, no model turn: the press is the
// owner's word) and the liaison's threads with that hub in the same feed.
// The core decides everything (core/busflow.py in the page server, modules/web/busweb.py); the page only asks. Nothing
// here depends on a new server: an older one has no /api/bus… and no "bus" in the snapshot's features — then the tab
// says so and the list has no group.
import { el, time, chevron } from "./dom.js";
import { post, banner } from "./api.js";
import { S, render } from "./state.js";
import { confirmDlg } from "./dialogs.js";
import { locale, t } from "./i18n.js";
import { keepOpen } from "./keepopen.js";
import { buildComposer } from "./composer.js";
import { menuAt, closePop } from "./groups.js";
import { spaceHint } from "./voice.js";
import { jumpDown } from "./jumpdown.js";

const PRESS_MAX = 5000;   // a press that never ended (released outside the window) does not hold the page back longer
const SEEN = "hub.busSeen";   // {contact: how many words of the other owner were seen} — the "new" count is the page's
const TOLD = "hub.busHeardTold";   // this browser's own marks were told to the core once (told0)
const HEARD = "hub.busHeard";   // {contact: the time of the hub's latest word (its owner's or its sessions') seen here}
const DAY = 24 * 3600;   // seconds: a word a hub sent on its own is in "Now" this long, as a session's (sidebar.js ownNews)
const V = { view: null, asked: 0, loading: false, job: null, jobAt: 0, texts: new Map(), open: new Set(), adopted: "", consent: false, paste: "",
  preview: null, previewFor: "", personFor: null, err: "", pasteOpen: false, nameDraft: null, inviteName: null,
  people: null, invitePerson: { pick: "", fresh: "" }, joinPerson: { pick: "", fresh: "" }, pickPerson: { pick: "", fresh: "" } };
const chats = new Map();      // contact -> {rows, at, loading, draft, confirm, sending, error}
// 503 — the page server reads the hub's journal anew (a restart, a loaded machine): not a refusal, the same request
// goes again in a moment and the tab says «Loading…», never a red line
const AGAIN = 2000;
const busy = (r) => r.status === 503;
// the same for a read with no answer at all (0: the server is being restarted) — never for start(): 0 may mean it arrived
const unread = (r) => busy(r) || (!!r.error && !r.status);
const shown = () => !!root?.isConnected;   // the tab is on screen: only then the page asks again by itself

export const busOn = () => (S.snap?.features || []).includes("bus");
// a link or a relay invitation in a text (core/busflow.KINDS): the prefix is enough to stop a send
export const busLinkIn = (text) => busOn() && /hub-link-1:|hub-relay-invite-1:/.test(String(text || ""));

// «Link this hub» from a session's field: the text goes into the paste field of Settings → Hubs, with its preview
export function pasteIntoHubs(text) {
  V.paste = text;
  V.pasteOpen = true;
  V.preview = null;
  localStorage.setItem("hub.settingsTab", "hubs");
  location.hash = "#/settings";
  look(text);
}
const canCopy = () => !!(window.isSecureContext && navigator.clipboard?.writeText);

async function act(op, body = {}) { return post("/api/bus/act", { op, ...body }); }

function seen(k = SEEN) { try { return JSON.parse(localStorage.getItem(k) || "{}") || {}; } catch { return {}; } }
function markSeen(name, n, k = SEEN) { const s = seen(k); if (s[name] !== n) { s[name] = n; localStorage.setItem(k, JSON.stringify(s)); } }
export const newIn = (c) => Math.max(0, (c.chat_in || 0) - (seen()[c.name] ?? 0));
// "Now" keeps a hub by the rules of sessions (sidebar.js): one that answered you (core: `answered` — an answer to your own
// words or to a request a session sent in your turn) until you open it, as "Answered you"; one that wrote anything else
// not seen (`heard`: its sessions' words too) for a day, as "Sent on their own". What was seen is the core's (`seen`:
// opened on any device) or this browser's mark, the later one. A server without `answered` counts every word for a day,
// one without `heard` its owner's words alone. marks: seen(HEARD), read once per list
const mark = (c, marks) => Math.max(marks[c.name] ?? 0, typeof c.seen === "number" ? c.seen : 0);
const answeredYou = (c, marks = seen(HEARD)) => typeof c.answered === "number" && c.answered > mark(c, marks);
const ownTill = (c, marks) => typeof c.heard === "number" && c.heard > mark(c, marks) ? c.heard + DAY : 0;
const fresh = (c, marks = seen(HEARD)) => {
  const now = Date.now() / 1000;
  if (typeof c.heard !== "number") return newIn(c) > 0 && (typeof c.last_in !== "number" || c.last_in > now - DAY);
  return answeredYou(c, marks) || ownTill(c, marks) > now;
};
// when the nearest hub's "on its own" day ends (ms from now; Infinity — none): the list redraws then (sidebar.js)
export const hubsDue = () => {
  const marks = seen(HEARD);
  return Math.min(Infinity, ...(S.snap?.bus?.contacts || []).filter((c) => !answeredYou(c, marks))
    .map((c) => ownTill(c, marks) * 1000 - Date.now()).filter((d) => d > 0));
};

const when = (ts) => {
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString() ? time(ts)
    : d.toLocaleDateString(locale, { day: "numeric", month: "short" }) + " " + time(ts);
};

// --- the view and the job ------------------------------------------------------------------------------------------

let loadLater = 0;
async function load(force = false) {
  const wait = 2000 - (Date.now() - V.asked);
  if (V.loading || (!force && wait > 0)) {   // asked a moment ago: once more after it — the last change is not lost
    if (!loadLater) loadLater = setTimeout(() => { loadLater = 0; load(); }, Math.max(wait, 300));
    return;
  }
  V.loading = true;
  V.asked = Date.now();
  const r = await act("view", V.consent ? { consent: true } : {});   // the consent names the relay: read it now
  V.loading = false;
  if (unread(r)) {   // the last view stays as it was; asked again in a moment
    if (!loadLater && shown()) loadLater = setTimeout(() => { loadLater = 0; load(); }, AGAIN);
    return;
  }
  if (!r.error) {
    V.view = r.data; V.err = "";
    // the public relay's address is being read from the install's pages: asked again in a moment
    if (r.data.public_relay_reading && !loadLater && shown()) loadLater = setTimeout(() => { loadLater = 0; load(); }, AGAIN);
    if (r.data.job && (!V.job || r.data.job.n >= V.job.n)) V.job = { ...V.job, ...r.data.job };
    if (V.job && !V.job.done) pollJob();
    // the page was reloaded after the link was made: its text comes with the job (only in that answer)
    else if (V.job?.what === "link" && V.job.done?.code === "ok" && V.job.done.id !== V.adopted) adopt((await post("/api/bus/job", {})).data?.job);
  } else V.err = r.error;
  redraw(!force);   // after the owner's own action — at once; a snapshot's refresh waits for his field
}

// a made invitation joins the list of open ones, opened (its text comes with the job, once): the form above does not change
function adopt(j) {
  if (j?.what !== "link" || j.done?.code !== "ok" || !j.done.id || j.done.id === V.adopted) return;
  V.adopted = j.done.id;   // the job is looked at once, with or without its text (a later view does not ask again)
  if (!j.value) return;
  V.texts.set(j.done.id, j.value);
  V.open.add(j.done.id);
}

let jobTimer = 0;
function pollJob() {
  if (jobTimer) return;
  jobTimer = setTimeout(async () => {
    jobTimer = 0;
    const r = await post("/api/bus/job", {});
    const j = r.data?.job;
    if (j) {
      V.job = j;
      adopt(j);
    }
    if (j && !j.done) pollJob(); else { load(true); render(); }
    redraw();
  }, 600);
}

// what → the body of its last press while that press is under way: a press again (a double click, a press during
// «Loading…») only replaces what goes, so one invitation is made, with the last label and term
const pressed = new Map();

async function start(body, tries = 0) {
  if (!tries) { const going = pressed.has(body.what); pressed.set(body.what, body); if (going) return; }
  body = pressed.get(body.what);
  V.err = "";
  const r = await post("/api/bus/start", body);
  // the server refused before doing anything: the owner's press goes again by itself for up to a minute
  if (busy(r) && tries < 30 && shown()) {
    V.waiting = true;
    redraw();
    return void setTimeout(() => start(body, tries + 1), AGAIN);
  }
  pressed.delete(body.what);
  V.waiting = false;
  if (r.data?.job) { V.job = r.data.job; if (body.what === "link") V.inviteName = null; pollJob(); }
  else if (r.data?.consent) V.consent = true;
  else V.err = r.error;
  redraw();
}

// --- pieces --------------------------------------------------------------------------------------------------------

const STEP_NAMES = () => ({ ssh: t("web.bus.step.ssh"), lib: t("web.bus.step.lib"), selfcheck: t("web.bus.step.selfcheck"),
  keys: t("web.bus.step.keys"), card: t("web.bus.step.card"), relay: t("web.bus.step.relay"), link: t("web.bus.step.link"),
  "join:run": t("web.bus.step.join_run"), "join:parsed": t("web.bus.step.parsed"), "join:joining": t("web.bus.step.joining"), "join:joined": t("web.bus.step.joined"),
  "join:contact": t("web.bus.step.contact"), "join:intro_sent": t("web.bus.step.intro_sent"),
  "join:linked": t("web.bus.step.linked") });

function steps(job) {
  const ul = el("ul", "bus-steps");
  const names = STEP_NAMES();
  for (const s of job.steps || []) {
    if (s.state === "skip") continue;
    const li = el("li", "bus-step " + s.state);
    li.dataset.step = s.step;
    li.append(el("span", "m", s.state === "ok" ? "✓" : s.state === "fail" ? "✕" : "◐"),
      el("span", null, names[s.step] || s.step));
    // the failed step's reason is the outcome's text as well (shown once, below)
    if (s.text && s.state !== "ok" && !s.step.startsWith("join:") && s.text !== job.done?.text) li.append(el("span", "why", s.text));
    ul.append(li);
  }
  if (!job.done) ul.append(el("li", "bus-step wait", t("web.bus.step.wait")));
  return ul;
}

function failure(done) {
  const box = el("div", "bus-fail");
  box.append(el("p", null, done.text || done.code));
  if (done.command) {
    box.append(el("p", "hint", t("web.bus.command_hint")));
    box.append(el("code", "bus-cmd", done.command));
  }
  return box;
}

function button(label, cls, onClick) {
  const b = el("button", "btn small " + (cls || ""), label);   // the size of the other Settings tabs' buttons; the fill marks the main one
  b.type = "button";
  b.addEventListener("click", async () => { b.disabled = true; try { await onClick(); } finally { b.disabled = false; } });
  return b;
}

// a link the owner copies: a read-only field that selects itself on a tap (http: no clipboard API, spec §3.4)
function linkBox(value) {
  const area = el("textarea", "bus-link");
  area.readOnly = true;
  area.value = value;
  area.rows = 3;
  area.setAttribute("aria-label", t("web.bus.link_label"));
  const all = () => { area.focus(); area.select(); area.setSelectionRange(0, value.length); };
  area.addEventListener("click", all);
  area.addEventListener("focus", all);
  return area;
}

function chip(text, cls) { return el("span", "bus-chip " + (cls || ""), text); }

// --- Settings → Hubs -----------------------------------------------------------------------------------------------

let root = null, pressedAt = 0;
// the release (or the end) of a press: the click has been handled by the next turn, then the refresh that waited is drawn
for (const type of ["pointerup", "pointercancel"])
  document.addEventListener(type, () => { if (pressedAt) setTimeout(() => { pressedAt = 0; if (root?.dataset.stale) redraw(true); }); }, true);
export function hubsSection() {
  root = el("div", "bus-tab settings-groups");
  root.addEventListener("focusout", () => { if (root.dataset.stale) setTimeout(() => redraw(true)); });
  // a press is under way: a refresh that waited for a list (the press took its focus away) must not draw the tab anew
  // before the release — the button the press began on is gone by then and the click reaches nobody
  root.addEventListener("pointerdown", () => { pressedAt = Date.now(); });
  load(true);
  redraw();
  return root;
}
export function refreshHubs() { load(); }

// quiet — a snapshot's refresh: it waits while the owner is in a field; the owner's own actions draw at once
function redraw(quiet = false) {
  if (!root) return;
  // the owner is in a field or a list: later (when he leaves it)
  const a = document.activeElement, typing = root.contains(a) && (a.tagName === "TEXTAREA" || a.tagName === "SELECT"
    || (a.tagName === "INPUT" && !["checkbox", "radio", "button"].includes(a.type)));
  if (quiet && (typing || Date.now() - pressedAt < PRESS_MAX)) return void (root.dataset.stale = "1");
  root.dataset.stale = "";
  // a button the owner reached with the keyboard stays focused through the redraw (the snapshot's refresh draws the tab anew)
  const home = (n) => { const c = n.closest("[data-part], [data-contact]"); return c ? (c.dataset.part || "") + "/" + (c.dataset.contact || "") : ""; };
  const was = a?.tagName === "BUTTON" && root.contains(a) ? { text: a.textContent, home: home(a), key: a.dataset.focusKey } : null;
  // a name field the owner is in keeps its focus and caret through a redraw (his own action elsewhere may cause one)
  const typed = a?.tagName === "INPUT" && a.dataset.keepFocus && root.contains(a) ? { key: a.dataset.keepFocus, from: a.selectionStart, to: a.selectionEnd } : null;
  root.replaceChildren(...tabNodes());
  if (typed) {
    const f = root.querySelector(`input[data-keep-focus="${typed.key}"]`);
    if (f) { f.focus({ preventScroll: true }); try { f.setSelectionRange(typed.from, typed.to); } catch { /* no caret */ } }
  }
  if (was) [...root.querySelectorAll("button")].find((b) => (was.key ? b.dataset.focusKey === was.key : b.textContent === was.text && home(b) === was.home))?.focus({ preventScroll: true });
}

function tabNodes() {
  if (!busOn()) return [el("p", "lead", t("web.bus.lead")), el("p", "hint", t("web.bus.old_server"))];
  const v = V.view;
  const head = el("div", "bus-head");
  head.append(el("h3", "day", t("web.bus.title")));
  if (v?.enabled) {
    head.append(chip(t("web.bus.on"), "ok"));
    head.append(button(t("web.bus.off"), "quiet", () => confirmDlg(t("web.bus.off_title"), t("web.bus.off_text"),
      t("web.bus.off"), async () => { const r = await act("off"); if (r.error) banner(r.error); load(true); })));
  }
  const out = [head];
  if (!v) { out.push(el("p", "hint", V.err || t("web.bus.loading"))); return out; }
  out.push(namePanel(v));
  if (v.enabled && v.mode) out.push(modePanel(v));   // a server of the previous release has no mode: no switch
  if (!(v.contacts || []).length) out.push(el("p", "lead", t("web.bus.lead")));
  if (v.enabled && !v.ready) out.push(failure({ text: t("web.bus.broken", { why: v.failed?.reason || v.failed?.code || "" }) }));
  // the linked hubs come first; «Make» and «Accept» an invitation are one quiet line under them
  if ((v.contacts || []).length) out.push(el("h3", "day", t("web.bus.contacts")), ...v.contacts.map((c) => contactCard(c, v)));
  // this hub's own check code: what the other owner types when the pair code is not there (the old way)
  if (v.me?.code && (v.contacts || []).length) out.push(el("p", "hint bus-mecode", t("web.bus.me_code", { code: v.me.code })));
  out.push(linkPanel(v), ...[pastePanel(v)].filter(Boolean));
  if (V.waiting) out.push(el("p", "hint", t("web.bus.loading")));
  else if (V.err) out.push(el("p", "bus-err", V.err));
  const joins = (v.joins || []).filter((j) => j.state === "active" || j.state === "cancelling");
  if (joins.length) out.push(el("h3", "day", t("web.bus.joins")), ...joins.map(joinRow));
  if ((v.links || []).length) out.push(el("h3", "day", t("web.bus.open_links")), linksList(v));
  const more = el("details", "set-more");
  keepOpen(more, "hubs:more");   // the tab is redrawn with every snapshot
  more.append(el("summary", null, t("web.bus.advanced")), el("p", "hint", t("web.bus.advanced_text")));
  if (v.me) more.append(el("p", "hint", t("web.bus.me", { label: v.me.label || "", fp: v.me.fp })));
  if (v.relay) more.append(el("p", "hint", t("web.bus.relay_is", { url: v.relay.url })));
  out.push(more);
  return out;
}

// this hub's own name for the other hubs: the one required setting. Until it is given, no invitation is made and none is
// accepted; the field is above them all and says why. The text is kept in the field through the page's redraws.
function namePanel(v) {
  const box = el("section", "bus-panel bus-hubname" + (v.name ? "" : " unset"));
  box.dataset.part = "name";
  box.append(el("b", null, t("web.bus.name_title")), el("p", "hint", v.name ? t("web.bus.name_hint") : t("web.bus.name_unset")));
  const f = el("input", "bus-hubname-in");
  f.type = "text";
  f.maxLength = 100;
  f.placeholder = t("web.bus.name_ph");
  f.value = V.nameDraft ?? v.name ?? "";
  f.setAttribute("aria-label", t("web.bus.name_title"));
  f.dataset.keepFocus = "name";
  const save = async () => {
    const text = f.value.trim();
    if (!text) return void banner(t("web.bus.name_empty"));
    if (text === v.name) { V.nameDraft = null; return; }
    const r = await act("name", { text });
    if (r.error) return void banner(r.error);
    V.nameDraft = V.inviteName = null;
    f.blur();   // the page redraws from the snapshot that carries the name: a field in use would make it wait
    banner(t("web.bus.name_saved"));
    load(true);
  };
  f.addEventListener("input", () => { V.nameDraft = f.value; });
  f.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
  const row = el("div", "bus-row");
  row.append(f, button(t("web.bus.name_save"), v.name ? "quiet" : "primary", save));
  box.append(row);
  return box;
}

// the liaison's mode: someone sits at this page (the liaison decides), or nobody does and the linked hubs run the hub —
// their words go to the hub session as the owner's task, forms are refused with whom to ask (spec d2505378, part D, remote)
function modePanel(v) {
  const remote = v.mode === "remote";
  const box = el("section", "bus-panel bus-mode");
  box.dataset.part = "mode";
  const l = el("label", "opt");
  const sw = el("input");
  sw.type = "checkbox";
  sw.checked = remote;
  sw.dataset.act = "bus-mode";
  l.append(sw, " ", el("span", null, t("web.bus.mode_remote")));
  box.append(l, el("p", "hint", t(remote ? "web.bus.mode_remote_on" : "web.bus.mode_remote_off")));
  sw.addEventListener("change", async () => {
    const want = sw.checked;
    sw.disabled = true;
    const r = await act("mode", { mode: want ? "remote" : "secretary" });
    sw.disabled = false;
    if (r.error) { sw.checked = !want; return void banner(r.error); }
    load(true);
  });
  return box;
}

// the name in this invitation alone: the hub's own, changeable here without touching the setting
function inviteName(v) {
  const f = el("input", "bus-invite-name");
  f.type = "text";
  f.maxLength = 100;
  f.value = V.inviteName ?? v.name ?? "";
  f.setAttribute("aria-label", t("web.bus.invite_name"));
  f.dataset.keepFocus = "invite";
  f.addEventListener("input", () => { V.inviteName = f.value; });
  const box = el("label", "bus-invite-field");
  box.append(el("span", null, t("web.bus.invite_name")), f, el("span", "hint", t("web.bus.invite_name_hint")));
  return box;
}
const inviteLabel = (v) => {
  const x = (V.inviteName ?? "").trim();
  return x && x !== v.name ? x : undefined;
};

// «Whose hub»: a person of the owner's contacts book, a new one, or nobody. The book is the owner's own (`hub contacts`);
// the core is told only the id and the name. `o` is the choice kept across redraws: {pick: "" | a name | NEW, fresh}.
const NEW = "\u0000new";
function loadPeople() {
  if (V.people !== null || V.peopleAsked) return;
  V.peopleAsked = true;
  act("people").then((r) => {
    // not read (no answer, the server busy): not an empty book — «Loading…» stays and the book is asked again
    if (unread(r)) return void setTimeout(() => { V.peopleAsked = false; if (shown()) redraw(true); }, AGAIN);
    V.people = r.data?.people || []; V.peopleAsked = false; redraw(true);
  });
}
function personField(o, { label, hint } = {}) {
  loadPeople();
  const box = el("label", "bus-person-field");
  const sel = el("select", "bus-person");
  sel.setAttribute("aria-label", t("web.bus.person_title"));
  sel.append(new Option(t("web.bus.person_nobody"), ""));
  if (V.people === null) { const o = new Option(t("web.bus.loading"), "\u0000loading"); o.disabled = true; sel.append(o); }
  for (const p of V.people || []) sel.append(new Option(p.name + (p.kinds.length ? " · " + p.kinds.join(", ") : ""), p.name));
  sel.append(new Option(t("web.bus.person_new"), NEW));
  sel.value = [...sel.options].some((x) => x.value === o.pick) ? o.pick : "";
  const fresh = el("input", "bus-person-new");
  fresh.type = "text";
  fresh.maxLength = 100;
  fresh.placeholder = t("web.bus.person_new_ph");
  fresh.value = o.fresh;
  fresh.hidden = o.pick !== NEW;
  fresh.dataset.keepFocus = "person-new";
  fresh.setAttribute("aria-label", t("web.bus.person_new_ph"));
  sel.addEventListener("change", () => { o.pick = sel.value; fresh.hidden = o.pick !== NEW; if (!fresh.hidden) fresh.focus(); });
  fresh.addEventListener("input", () => { o.fresh = fresh.value; });
  box.append(el("span", null, label || t("web.bus.person_title")), sel, fresh, el("span", "hint", hint || t("web.bus.person_hint")));
  return box;
}
// what the page sends: nothing, an existing record by its name, or a new person
// «a new person» chosen and no name given, or a link / an invitation / a card (core/busflow.KINDS) pasted in its place: ask, do not guess
const personBad = (o) => o.pick === NEW && (!o.fresh.trim() || /hub-link-1:|hub-relay-invite-1:|hub-bus-card-1:/i.test(o.fresh));
const personSpec = (o) => o.pick === NEW ? (o.fresh.trim() ? { new: o.fresh.trim() } : undefined) : o.pick ? { name: o.pick } : undefined;
// a hub's name with its person beside it, wherever the page shows the hub
const hubName = (c) => c.person ? c.label + " · " + c.person : c.label;

function linkPanel(v) {
  const box = el("section", "bus-panel");
  box.dataset.part = "link";
  const job = V.job && V.job.what === "link" ? V.job : null;
  if (job && !job.done) {
    box.append(el("b", null, t("web.bus.preparing")), steps(job), el("p", "hint", t("web.bus.preparing_hint")));
    return box;
  }
  if (job?.done && job.done.code !== "ok") {
    box.append(el("b", null, t("web.bus.link_failed")), steps(job));
    // a hub that is on no relay yet: the one way out, in the page's words (not the core's with a terminal command)
    box.append(job.done.code === "no_relay" ? el("p", null, t("web.bus.need_invite")) : failure(job.done));
  }
  const taken = V.job?.what === "join" && V.job.done?.code === "ok" && V.job.done.url ? V.job.done : null;
  if (V.consent && !v.enabled) {
    box.dataset.part = "consent";
    box.append(el("b", null, t("web.bus.consent_title")));
    const ul = el("ul", "bus-consent");
    for (const k of ["web.bus.consent_keys", "web.bus.consent_lib", "web.bus.consent_relay", "web.bus.consent_nothing"])
      ul.append(el("li", null, t(k)));
    if (v.public_relay) ul.append(el("li", null, t("web.bus.public_relay", { url: v.public_relay })));
    const row = el("div", "bus-row");
    row.append(button(t("web.bus.consent_go"), "primary", () => personBad(V.invitePerson) ? banner(t("web.bus.person_new_ph")) : start({ what: "link", consent: true, ttl: ttlChoice, label: inviteLabel(v), person: personSpec(V.invitePerson) })),
      button(t("web.bus.cancel"), "quiet", () => { V.consent = false; redraw(); }));
    box.append(ul, row);
    return box;
  }
  // the first hub of a relay has just taken its invitation: the next step is the button right under this line
  if (taken) box.append(el("div", "bus-banner", t("web.bus.relay_joined", { url: taken.url })));
  const row = el("div", "bus-row");
  const make = button(t("web.bus.link"), "primary", () => (v.enabled ? personBad(V.invitePerson) ? banner(t("web.bus.person_new_ph")) : start({ what: "link", ttl: ttlChoice, label: inviteLabel(v), person: personSpec(V.invitePerson) })
    : (V.consent = true, redraw(), load(true))));
  if (!v.name) make.disabled = true;   // nothing is made without the hub's name (the field above says so)
  row.append(make, ttlSelect());
  // «Accept an invitation» is always at hand: the field itself while no hub is linked, later a button of this row
  if (pasteFolded(v)) row.append(button(t("web.bus.paste_title"), "quiet", () => { V.pasteOpen = true; redraw(); focusPaste(); }));
  box.append(row);
  box.append(v.name ? inviteName(v) : el("p", "hint", t("web.bus.need_name")));
  if (v.name) box.append(personField(V.invitePerson));
  if (!(v.contacts || []).length) box.append(el("p", "hint", t("web.bus.link_hint")));
  // no relay yet: «Link» takes the public one this hub was installed with — said here, with the way to one's own
  if (v.enabled && v.public_relay) box.append(el("p", "hint", t("web.bus.public_relay", { url: v.public_relay })));
  return box;
}

// the field of «Accept an invitation» is shown at once while no hub is linked; later — by its button, or while it is in use
function pasteFolded(v) {
  if (!(v.contacts || []).length || V.pasteOpen || V.paste || V.preview) return false;
  const j = V.job?.what === "join" ? V.job : null;
  return !(j && (!j.done || j.done.code !== "ok"));
}
function focusPaste() { root?.querySelector(".bus-paste")?.focus({ preventScroll: true }); }

let ttlChoice = "24h";
function ttlSelect() {
  const s = el("select", "bus-ttl-pick");
  s.setAttribute("aria-label", t("web.bus.ttl_label"));
  for (const [k, label] of [["1h", t("web.bus.ttl_1h")], ["24h", t("web.bus.ttl_24h")], ["7d", t("web.bus.ttl_7d")]]) {
    const o = el("option", null, label);
    o.value = k;
    o.selected = k === ttlChoice;
    s.append(o);
  }
  s.addEventListener("change", () => { ttlChoice = s.value; });
  return s;
}

let previewTimer = 0;
function pastePanel(v) {
  if (pasteFolded(v)) return null;
  const box = el("section", "bus-panel");
  box.dataset.part = "paste";
  const job = V.job && V.job.what === "join" ? V.job : null;
  if (job && !job.done) {
    box.append(el("b", null, t("web.bus.joining", { label: job.steps.find((s) => s.step === "join:run")?.text || "" })),
      steps(job), el("p", "hint", t("web.bus.joining_hint")));
    return box;
  }
  if (job?.done) {
    const d = job.done;
    // «Linked: …» is a toast (busNews) and the contact's card right below: no banner of its own here
    if (d.code === "waiting" || d.code === "queued") box.append(el("p", "bus-wait", d.text));
    else if (d.code !== "ok") box.append(steps(job), failure(d));
  }
  const area = el("textarea", "bus-paste");
  area.dataset.keep = "1";
  area.rows = 2;
  area.placeholder = t("web.bus.paste_ph");
  area.setAttribute("aria-label", t("web.bus.paste_label"));
  area.value = V.paste;
  area.addEventListener("input", () => {
    V.paste = area.value;
    if (area.value) V.pasteOpen = true;   // emptied by hand: the field stays
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => look(area.value), 250);
  });
  box.append(el("b", null, t("web.bus.paste_title")), area);
  const p = V.preview && V.previewFor === V.paste ? V.preview : null;
  if (p) box.append(previewNode(p, v));
  else if (!(v.contacts || []).length) box.append(el("p", "hint", t("web.bus.paste_hint")));
  else box.append(button(t("web.bus.cancel"), "quiet", () => { V.pasteOpen = false; V.paste = ""; V.preview = null; redraw(); }));
  return box;
}

async function look(text) {
  if (!text.trim()) { V.preview = null; V.previewFor = ""; return redraw(); }
  const r = await act("preview", { text });
  if (text !== V.paste) return;
  if (unread(r)) setTimeout(() => { if (V.paste === text && shown()) look(text); }, AGAIN);   // the same text, in a moment
  V.preview = unread(r) ? { wait: true } : r.error ? { error: r.error, command: r.data?.command } : r.data;
  V.previewFor = text;
  const had = document.activeElement === root?.querySelector(".bus-paste");
  redraw();   // the preview shows while the field has focus; the field comes back focused
  if (had) root?.querySelector(".bus-paste")?.focus({ preventScroll: true });
}

function previewNode(p, v) {
  const box = el("div", "bus-preview");
  if (p.wait) { box.append(el("p", "hint", t("web.bus.loading"))); return box; }
  if (p.error) { box.append(el("p", "bus-err", p.error)); if (p.command) box.append(el("code", "bus-cmd", p.command)); return box; }
  // one field, one button: a hub's invitation and the first hub's one (a relay's) are accepted the same way
  const invite = p.kind === "invite";
  box.append(el("p", "bus-q", invite ? t("web.bus.invite_q", { url: p.relay }) : t("web.bus.link_q", { label: p.label })));
  const dl = el("dl", "bus-kv");
  const kv = (k, val) => dl.append(el("dt", null, k), el("dd", null, val));
  if (invite) {
    kv(t("web.bus.kv_relay"), t("web.bus.kv_relay_text", { url: p.relay }));
    if ((p.needs || []).includes("enable")) kv(t("web.bus.kv_what"), t("web.bus.kv_what_enable_first"));
  } else {
    if (p.relay) kv(t("web.bus.kv_relay"), t("web.bus.kv_relay_text", { url: p.relay }));
    kv(t("web.bus.kv_what"), (p.needs || []).includes("enable") ? t("web.bus.kv_what_enable") : t("web.bus.kv_what_ready"));
    kv(t("web.bus.kv_rights"), t("web.bus.kv_rights_text"));
    kv(t("web.bus.kv_term"), when(p.exp));
  }
  const row = el("div", "bus-row");
  const join = button(t("web.bus.join"), "primary", () => { if (personBad(V.joinPerson)) return void banner(t("web.bus.person_new_ph")); const text = V.paste, person = personSpec(V.joinPerson); V.paste = ""; V.pasteOpen = false; V.preview = null; V.joinPerson = { pick: "", fresh: "" }; return start({ what: "join", text, person }); });
  if (!v.name) join.disabled = true;   // the pasted text stays while the hub gets its name
  row.append(join, button(t("web.bus.cancel"), "quiet", () => { V.paste = ""; V.pasteOpen = false; V.preview = null; redraw(); }));
  box.append(dl, ...(invite ? [] : [personField(V.joinPerson)]), row);
  if (!v.name) box.append(el("p", "hint", t("web.bus.need_name")));
  if (invite) box.append(el("p", "hint", t("web.bus.invite_hint")));
  return box;
}

function joinRow(j) {
  const box = el("div", "bus-panel bus-joinrow");
  const steps = { joining: "web.bus.step.joining", joined: "web.bus.step.joined", contact: "web.bus.step.contact",
    intro_sent: "web.bus.step.wait_a" };
  box.append(el("span", null, t("web.bus.join_with", { label: j.label || "" })),
    el("span", "hint", t(steps[j.step] || "web.bus.step.wait_a")),
    button(t("web.bus.cancel"), "quiet", async () => { const r = await act("cancel", { id: j.id }); if (r.error) banner(r.error); load(true); }));
  return box;
}

// «Revoke» of one invitation: it is the one that was sent, so the owner confirms (the other hub may be holding it)
function revokeButton(l) {
  const b = button(t("web.bus.revoke"), "quiet danger", () => confirmDlg(t("web.bus.revoke_title"), t("web.bus.revoke_text"),
    t("web.bus.revoke"), async () => {
      const r = await act("revoke", { id: l.id });
      if (r.error) banner(r.error);
      else { V.texts.delete(l.id); V.open.delete(l.id); }
      load(true);
    }));
  b.dataset.focusKey = "revoke:" + l.id;   // the keyboard's place among several invitations
  return b;
}

// the open invitations, each in its own place: «Show» opens one in its own line (its text, «Copy», the hints) and
// «Hide» folds it back; nothing moves to another block, the form above is the same. One just made is opened at once
function linksList(v) {
  const box = el("div", "bus-panel");
  box.dataset.part = "links";
  for (const id of [...V.texts.keys()]) if (id !== V.adopted && !(v.links || []).some((l) => l.id === id)) { V.texts.delete(id); V.open.delete(id); }   // used or expired: the text is not kept (the one just made waits for the view that has it)
  for (const l of [...(v.links || [])].reverse()) {   // the newest first: the one just made is right under the form
    const item = el("div", "bus-inv");
    const row = el("div", "bus-row");
    row.dataset.link = l.id;
    const text = V.texts.get(l.id), opened = V.open.has(l.id) && !!text;
    row.append(inviteWho(l));
    const show = button(opened ? t("web.bus.hide") : t("web.bus.show"), "quiet", async () => {
      if (opened) V.open.delete(l.id);
      else {
        if (!V.texts.has(l.id)) {
          const r = await act("link_text", { id: l.id });
          if (!r.data?.value) return void banner(r.error);
          V.texts.set(l.id, r.data.value);
        }
        V.open.add(l.id);
      }
      redraw();
    });
    show.dataset.focusKey = "show:" + l.id;
    const acts = el("span", "bus-acts");   // the two buttons wrap together on a narrow screen
    acts.append(show, revokeButton(l));
    row.append(acts);
    item.append(row);
    if (opened) item.append(inviteOpen(l, text));
    box.append(item);
  }
  return box;
}

// whose invitation it is, in the line: the person of the book it was made for is the main word (else the name it carries for
// that invitation alone); the term, that name beside a person, and the id (small, mono) are the line under it. An invitation
// made before these fields, or with neither: «Invitation»
function inviteWho(l) {
  const box = el("span", "bus-inv-who");
  const sub = el("span", "hint");
  box.title = l.id;
  box.append(el("span", "bus-inv-name", l.person || l.label || t("web.bus.link_plain")), sub);
  sub.append(t("web.bus.link_until", { until: when(l.exp) }) + (l.person && l.label ? " · " + t("web.bus.link_as", { label: l.label }) : "") + " · ",
    el("span", "bus-mono", l.id.slice(0, 4) + "…" + l.id.slice(-4)));
  return box;
}

function inviteOpen(l, text) {
  const body = el("div", "bus-inv-open");
  body.append(linkBox(text));
  const row = el("div", "bus-row");
  if (canCopy()) {
    const copy = button(t("web.bus.copy"), "primary", async () => {
      try { await navigator.clipboard.writeText(text); banner(t("web.bus.copied")); } catch { banner(t("web.bus.copy_failed")); }
    });
    copy.dataset.focusKey = "copy:" + l.id;
    row.append(copy);
  } else row.append(el("span", "hint", t("web.bus.select_hint")));
  row.append(el("span", "hint bus-ttl", t("web.bus.link_term", { until: when(l.exp) })));
  body.append(row, el("p", "hint", t("web.bus.link_send_hint")));
  if (!l.posted && l.invite) body.append(el("p", "hint", t("web.bus.link_unposted")));
  body.append(el("p", "bus-wait", t("web.bus.link_waiting")));
  return body;
}

// the pair code and «Matches» (spec §3.6, §5.2): the same line on both pages; no code — the old way, the code is typed.
// Only until the code is compared: after it the code is a small line by the name (codeNote)
function codeRow(c, after) {
  const row = el("div", "bus-row bus-code");
  if (c.code) row.append(el("span", "hint", t("web.bus.code")), el("span", "bus-code-line", c.code));
  // our introduction went, the other hub's answer carries the code: nothing to type or compare before it (an older
  // server does not say «code_wait» — the field stays as it was)
  if (!c.code && c.code_wait) {
    row.append(el("span", "hint", t("web.bus.code_wait")));
    return row;
  }
  let typed = null;
  if (!c.code) {
    typed = el("input", "bus-code-in");
    typed.dataset.keep = "1";
    typed.placeholder = t("web.bus.code_ph");
    typed.setAttribute("aria-label", t("web.bus.code_ph"));
    row.append(typed);
  }
  row.append(button(t("web.bus.matches"), "primary", async () => {
    const r = await act("verify", { name: c.name, ...(typed ? { code: typed.value } : {}) });
    if (r.error) banner(r.error); else banner(t("web.bus.verified_now", { label: c.label }));
    load(true); after?.();
  }));
  return row;
}

// one status, said once: «code compared» (or «not compared»); the rule of the hub is its tooltip
function statusChip(c) {
  const chipNode = c.verified ? chip(t("web.bus.verified"), "ok") : chip(t("web.bus.unverified"), "warn");
  chipNode.title = c.verified ? t("web.bus.rule_verified") : t("web.bus.rule_unverified");
  return chipNode;
}

// the owner's own name for a hub, kept only here (the other hub is not told): the name turns into a field in place;
// Enter saves, and so does leaving the field (a tap elsewhere, the phone's keyboard closed with no Enter); Esc leaves it,
// an empty field brings back what the hub calls itself
function renameIn(node, c, after, fmt = (l) => l) {
  if (!node.isConnected) return;   // the field is already open: a second press does not open another one
  const f = el("input", "bus-rename");
  f.type = "text";
  f.maxLength = 100;
  f.value = c.alias || "";
  f.placeholder = c.stated || c.label;
  f.dataset.keep = "1";
  f.setAttribute("aria-label", t("web.bus.rename_label"));
  f.enterKeyHint = "done";
  let busy = false, left = false;
  const save = async (refocus = true) => {
    if (busy || left) return;
    busy = true; f.disabled = true;
    const r = await act("rename", { name: c.name, alias: f.value });
    // a refusal keeps the field with the typed text; the focus goes back only for Enter — after leaving the field it would
    // trap the owner in it (a phone has no Esc)
    if (r.error) { busy = false; f.disabled = false; banner(r.error); if (refocus) f.focus(); return; }
    node.textContent = fmt(f.value.trim() || c.stated || c.label);   // at once: the view below is asked again after this
    f.replaceWith(node);
    for (let i = 0; V.loading && i < 50; i++) await new Promise((r) => setTimeout(r, 100));
    await load(true); after?.();
  };
  f.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    if (e.key === "Enter") { e.preventDefault(); save(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if (!busy) { left = true; f.replaceWith(node); } }
  });
  // leaving the field saves what is typed; an untouched name only closes the field. The window losing the focus is no
  // leaving (the field stays the active element: the owner comes back to it)
  f.addEventListener("blur", () => {
    if (busy || left || document.activeElement === f) return;
    if (f.value.trim() === (c.alias || "")) { left = true; f.replaceWith(node); } else save(false);
  });
  node.replaceWith(f);
  f.focus();
  f.select();
}

// a right click or a long press on a hub's row in the list (groups.js): the same menu as the sessions' rows. «Rename»
// opens the hub's page with the name turned into a field (the list is redrawn on every snapshot — a field there would
// not live); «Link to a contact» — the person of the contacts book on the hub's card in Settings
const openPerson = (name) => {
  const c = (S.snap?.bus?.contacts || []).find((x) => x.name === name);
  localStorage.setItem("hub.settingsTab", "hubs");
  V.personFor = name;
  V.pickPerson = { pick: c?.person || "", fresh: "" };
  location.hash = "#/settings";
};
export function hubMenu(anchor, name) {
  const c = (S.snap?.bus?.contacts || []).find((x) => x.name === name);
  if (!c) return;
  menuAt(anchor, c.label, [
    { label: t("web.bus.rename"), onClick: () => { closePop(); V.renameOn = name; if (location.hash === "#/hub/" + encodeURIComponent(name)) refreshHubView(); else location.hash = "#/hub/" + encodeURIComponent(name); } },
    { label: t("web.bus.person_link"), onClick: () => { closePop(); openPerson(name); } },
  ]);
}

const renameBtn = (c, node, after, fmt) => button(t("web.bus.rename"), "quiet", () => renameIn(node, c, after, fmt));

function contactCard(c, v) {
  const box = el("section", "bus-panel bus-contact");
  box.dataset.contact = c.name;
  const top = el("div", "bus-row bus-chead");
  const nm = el("span", "bus-name", c.label);
  top.append(nm, renameBtn(c, nm), statusChip(c));
  if (c.person) top.append(el("span", "bus-person-name", "· " + c.person));
  if (c.alias && c.stated) top.append(el("span", "hint bus-stated", t("web.bus.stated", { label: c.stated })));
  if (v?.enabled && v?.mode === "remote" && c.verified && c.status === "known" && !c.quarantine) top.append(chip(t("web.bus.owner_chip"), "ok"));
  if (c.status === "muted") top.append(chip(t("web.bus.muted"), "mute"));
  if (c.quarantine) top.append(chip(t("web.bus.quarantine"), "warn"));
  if (c.verified && c.code) top.append(el("span", "bus-code-line quiet", c.code));   // compared: small, for the record
  const acts = el("span", "bus-acts");
  acts.append(button(t("web.bus.write"), "primary", () => { location.hash = "#/hub/" + encodeURIComponent(c.name); }),
    button(t("web.bus.unlink"), "quiet danger", () => askUnlink(c)));
  top.append(acts);
  box.append(top);
  if (!c.verified) {
    box.append(codeRow(c));
    if (!c.code_wait) box.append(el("p", "hint", t(c.code ? "web.bus.code_hint" : "web.bus.code_hint_typed")));
  }
  box.append(personRow(c));
  // the person is the owner's deputy (core/deputies.py): their area in his words; set in the terminal or by the hub
  if (c.deputy) {
    const d = el("div", "hint bus-deputy", t("web.bus.deputy", { zone: c.deputy.zone })
      + (c.deputy.not ? t("web.bus.deputy_not", { nope: c.deputy.not }) : "")
      + (c.deputy.off ? t("web.bus.deputy_off", { why: c.deputy.off }) : ""));
    box.append(d);
  }
  return box;
}

export function askUnlink(c, after) {
  confirmDlg(t("web.bus.unlink_title", { label: c.label }), t("web.bus.unlink_text"), t("web.bus.unlink"), async () => {
    const r = await act("unlink", { name: c.name });
    if (r.error) banner(r.error); else banner(t("web.bus.unlinked", { label: c.label }));
    load(true); render(); after?.();
  });
}

// whose hub this is (a person of the contacts book): the line on the hub's card, «Link to a contact» opens the choice
function personRow(c) {
  const box = el("div", "bus-personrow");
  box.dataset.person = c.name;
  const line = el("div", "bus-row");
  line.append(el("span", null, c.person ? t("web.bus.person_is", { name: c.person }) : t("web.bus.person_not")));
  if (V.personFor !== c.name) {
    line.append(button(t(c.person ? "web.bus.person_change" : "web.bus.person_link"), "quiet", () => {
      V.personFor = c.name;
      V.pickPerson = { pick: c.person && (V.people || []).some((p) => p.name === c.person) ? c.person : "", fresh: "" };
      redraw();
    }));
    box.append(line);
    return box;
  }
  box.append(line, personField(V.pickPerson, { label: t("web.bus.person_title"), hint: t("web.bus.person_hint") }));
  const row = el("div", "bus-row");
  row.append(button(t("web.bus.save"), "primary", async () => {
    if (personBad(V.pickPerson)) return void banner(t("web.bus.person_new_ph"));
    const spec = personSpec(V.pickPerson);
    const r = await act("person", { name: c.name, person: spec === undefined ? null : spec });
    if (r.error) return void banner(r.error);
    V.personFor = null;
    V.people = null;
    banner(t(spec === undefined ? "web.bus.person_cleared" : "web.bus.person_saved", { label: c.label }));
    load(true);
  }), button(t("web.bus.cancel"), "quiet", () => { V.personFor = null; redraw(); }));
  box.append(row);
  return box;
}

// --- the session list's «Hubs» group -------------------------------------------------------------------------------

// open and fold come from the list (sidebar.js): the heading folds like the other groups, by the same remembered state;
// kept — the "Now" tab: only the hubs waiting for you or with new messages (eleven always-there rows made it long) and
// those kept(c) holds (the open one, the just viewed: a row must not vanish on click), the rest are in "All"; none — no group
export function hubsGroup(open, fold, kept = null) {
  const marks = seen(HEARD);
  told0(marks);
  const list = (S.snap?.bus?.contacts || []).filter((c) => !kept || c.waiting || fresh(c, marks) || kept(c));
  if (!busOn() || !list.length) return null;
  const box = el("section", "sgroup asec bus-group");
  box.dataset.sec = "hubs";   // not data-g: session rows are not dropped here (groups.js)
  const h = el("button", "sgroup-h");
  h.type = "button";
  h.setAttribute("aria-expanded", String(open));
  h.append(chevron(open), el("span", "gt", t("web.bus.group")), el("span", "gcount", String(list.length)));
  if (!open) {   // folded: a dot if a hub is waiting for the owner or has something new (as on a folded group)
    const wants = list.some((c) => c.waiting), news = list.some((c) => fresh(c, marks));
    if (wants || news) {
      const d = el("span", wants ? "fdot wants" : "fdot");
      d.title = t(wants ? "web.sidebar.group_wants" : "web.sidebar.group_fresh");
      h.append(d);
    }
  }
  h.addEventListener("click", () => fold(!open));
  box.append(h);
  if (!open) return box;
  const ul = el("ul");
  for (const c of list) {
    const li = el("li");
    li.dataset.hub = c.name;
    const a = el("a", "srow bus-srow");
    a.href = "#/hub/" + encodeURIComponent(c.name);
    if (S.selected === ":hub:" + c.name) a.setAttribute("aria-current", "true");
    const n = newIn(c);
    const sub = c.person ? [c.person, c.verified ? t("web.bus.verified") : t("web.bus.unverified")]
      : [c.verified ? t("web.bus.verified") : t("web.bus.unverified")];
    // first, as "Answered you" of the sessions: a narrow list cuts the line's end, not this
    if (answeredYou(c, marks)) sub.unshift(t("web.bus.answered"));
    if (n) sub.push(t("web.bus.n_new", { count: n }));
    if (c.waiting) sub.push(t("web.bus.n_waiting", { count: c.waiting }));
    const txt = el("span", "txt"), g = el("span", "glyph", "↔");
    g.dataset.s = c.verified ? "idle" : "dialog";
    txt.append(el("span", "nm", c.label), el("span", "lb", sub.join(" · ")));
    a.append(g, txt);
    if (fresh(c, marks)) a.append(el("span", "dot"));
    li.append(a);
    ul.append(li);
  }
  box.append(ul);
  return box;
}

// what changed in the contacts since the last snapshot: «Linked», «Unlinked», a new word of the other owner
let known = null;
export function busNews() {
  const list = S.snap?.bus?.contacts || [];
  const now = new Map(list.map((c) => [c.name, c]));
  if (known) {
    for (const [n, c] of now) {
      const was = known.get(n);
      if (!was) banner(t("web.bus.linked_to", { label: c.label }));
      else if (c.chat_in > was.chat_in && S.selected !== ":hub:" + n) banner(t("web.bus.new_word", { label: c.label }));
    }
    for (const [n, c] of known) if (!now.has(n)) banner(t("web.bus.gone", { label: c.label }));
  }
  known = now;
}

// --- a contact's page: the owners' chat and the liaison's threads ----------------------------------------------------

const views = new Map();   // contact -> its view node (kept across snapshots: the field keeps its text and focus)
const told = new Map();    // contact -> the word the core is being told is seen (hubView runs on every snapshot)
// what this browser read before the core kept it (the release before): told once, so the phone does not bring those hubs
// back; a server without `seen` — not yet (it is told after the update)
function told0(marks) {
  const list = S.snap?.bus?.contacts || [];
  if (!busOn() || localStorage.getItem(TOLD) || !list.some((c) => typeof c.seen === "number")) return;
  localStorage.setItem(TOLD, "1");
  for (const c of list) if ((marks[c.name] ?? 0) > 0) tellSeen(c, marks[c.name]);
}
// the core's mark of what the owner read of a hub (every device sees it), once per word and only from a tab he looks at
// (a hub page left open behind does not read the answer for him); a refusal (the server reads the journal anew) — again
// with the next snapshot. A server without `seen` keeps only this browser's mark
function tellSeen(c, ts) {
  if (typeof c.seen !== "number" || c.seen >= ts || told.get(c.name) === ts || document.visibilityState !== "visible") return;
  told.set(c.name, ts);
  post("/api/seen", { session: ":hub:" + c.name, ts }).then((r) => { if (r.error && told.get(c.name) === ts) told.delete(c.name); });
}

export function hubView(name) {
  const c = (S.snap?.bus?.contacts || []).find((x) => x.name === name);
  let w = views.get(name);
  if (!w) { w = buildView(name); views.set(name, w); }
  w.update(c);
  if (c) markSeen(name, c.chat_in || 0);
  if (typeof c?.heard === "number" && document.visibilityState === "visible") {   // a page left open behind reads nothing
    markSeen(name, c.heard, HEARD);
    tellSeen(c, c.heard);
  }
  return w.parts;   // the head, the feed and the field — rows of main's grid, like a session's
}

function buildView(name) {
  const head = el("div", "head bus-vhead"), feed = el("div", "feed"), inner = el("div", "feed-inner bus-feed");
  feed.append(inner);
  const back = el("button", "btn quiet back", t("web.nav.all_sessions"));
  back.type = "button";
  back.addEventListener("click", () => { location.hash = "#/sessions"; });
  const title = el("h2"), chips = el("span", "bus-hchips"), tools = el("span", "bus-htools");
  head.append(back, title, chips, tools);
  // the field is the session field's own component (composer.js): the same look, Enter/Shift+Enter, voice; the bus carries
  // text only, so no files. The words go as «Tell the hub» (the owner's word, no card); a key-like text comes back with
  // the reason and goes on the second Enter with the text as it is
  const dock = el("div", "dock bus-dock"), dockIn = el("div", "dock-inner");
  const st = chats.get(name) || { rows: null, at: 0 };
  chats.set(name, st);
  const label = () => (S.snap?.bus?.contacts || []).find((x) => x.name === name)?.label || name;
  const composer = buildComposer({
    key: "hub:" + name, files: false, cls: "bus-composer",
    label: t("web.bus.write_to", { label: label() }),
    placeholder: t("web.dock.message_ph") + spaceHint(),
    hint: t("web.bus.to_hub_hint", { label: label() }),
    body: (text) => ({ session: ":hub:" + name, text, attachments: [] }),
    send: async (body) => {
      const r = await act("chat", { name, text: body.text, ...(st.again === body.text ? { confirm: true } : {}) });
      st.again = r.data?.confirm ? body.text : null;
      if (r.data?.ok) { history(name, true); return { ok: true, receipt: "accepted" }; }
      if (!r.data) return { error: r.error || t("web.api.no_answer"), receipt: "unknown" };   // the same text goes again with the same id
      const why = String(r.error || t("web.api.rejected")).trim();   // the texts of the page have no closing stop: the next sentence needs one
      return { error: r.data.confirm ? `${/[.!?…]$/.test(why) ? why : why + "."} ${t("web.bus.send_again")}` : why, receipt: "refused" };
    },
  });
  const unchecked = el("p", "hint bus-unchecked", t("web.bus.write_unchecked"));
  dockIn.append(composer.node, unchecked);
  dock.append(dockIn);
  const form = dock;
  let sig = "";
  const update = (c, fetch = true) => {
    if (!c) {
      title.textContent = name;
      chips.replaceChildren(chip(t("web.bus.not_linked"), "mute"));
      tools.replaceChildren();
      form.hidden = true;
      inner.replaceChildren(el("p", "hint", t("web.bus.not_linked_hint")));
      return;
    }
    form.hidden = false;
    title.textContent = "↔ " + hubName(c);
    const open = !!c.verified && c.status === "known";   // words for a hub's liaison wait for the check, and for a muted hub there are none
    composer.node.hidden = !open;
    unchecked.hidden = open;
    unchecked.textContent = t(c.verified ? "web.bus.write_closed" : "web.bus.write_unchecked");
    composer.update({ label: t("web.bus.write_to", { label: c.label }), placeholder: t("web.dock.message_ph") + spaceHint(),
      hint: t("web.bus.to_hub_hint", { label: c.label }) });
    const now = JSON.stringify([c.verified, c.label, c.code, c.code_wait, c.alias, c.stated, c.person]);
    if (now !== sig) {
      sig = now;
      chips.replaceChildren(statusChip(c));
      if (c.alias && c.stated) chips.append(chip(t("web.bus.stated", { label: c.stated }), "mute"));
      const person = button(t(c.person ? "web.bus.person_change" : "web.bus.person_link"), "quiet", () => openPerson(name));
      tools.replaceChildren(renameBtn(c, title, null, (l) => "↔ " + (c.person ? l + " · " + c.person : l)),
        person, button(t("web.bus.unlink"), "quiet danger", () => askUnlink(c)));
      if (!c.verified) {
        head.querySelector(".bus-code")?.remove();
        head.append(codeRow(c));
      } else head.querySelector(".bus-code")?.remove();
    }
    if (V.renameOn === name) {   // «Rename» from the list's menu: the field opens once the page is on the screen
      V.renameOn = null;
      let tries = 0;
      const open = () => { if (title.isConnected) renameIn(title, c, null, (l) => "↔ " + (c.person ? l + " · " + c.person : l)); else if (tries++ < 40) requestAnimationFrame(open); };
      open();
    }
    if (fetch) history(name);   // a new snapshot; a redraw after the history itself asks for nothing
    draw(inner, st, c);
  };
  st.redraw = () => update((S.snap?.bus?.contacts || []).find((x) => x.name === name), false);
  st.sync = follow(feed, st);
  // the button, once it is on the page, stands among the parts: main's placeNodes drops what is not in the list
  return { get parts() { return [head, feed, ...(st.jump.node.parentNode ? [st.jump.node] : []), form]; }, update };
}

// Where the feed stays. At the end — it follows the new words. Scrolled up — it keeps the place, also when the owner goes
// to another page and comes back: the page takes the feed's node off, and a node taken off forgets its scroll, so the
// place is kept here and put back when the node is on the page again (the observer sees it get its size). Not at the end —
// "To the latest" with the number of messages that came after the owner last saw the end (their own words do not count).
function follow(feed, st) {
  st.jump = jumpDown(() => { st.away = false; feed.scrollTop = feed.scrollHeight; sync(); });
  const live = () => feed.isConnected && feed.clientHeight > 0;
  const near = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  const mine = (r) => r.kind === "chat" && r.way === "out";
  let on = false;   // the node has been on the page with a size since the last restore
  function sync() {
    const rows = st.rows || [];
    if (!st.away || !st.seen) st.seen = new Set(rows.map((r) => r.id));   // at the end everything is seen
    const n = st.away && live() ? rows.filter((r) => !st.seen.has(r.id) && !mine(r)).length : 0;
    if (n) st.jump.show(n, feed.parentNode);
    else st.jump.hide();
  }
  feed.addEventListener("scroll", () => {
    if (!on || !live()) return;   // taking the node off the page resets the scroll: that is not the owner's move
    st.top = feed.scrollTop;
    st.away = !near();
    sync();
  });
  new ResizeObserver(() => {
    const now = live();
    if (now && !on) feed.scrollTop = st.away ? st.top : feed.scrollHeight;   // the node is back (or first on the page)
    on = now;
    sync();
  }).observe(feed);
  return sync;
}

async function history(name, force = false) {
  const st = chats.get(name);
  if (!st) return;
  const wait = 1500 - (Date.now() - st.at);
  if (st.loading || (!force && wait > 0)) {   // asked a moment ago: once more after it — a late receipt is not lost
    if (!st.later) st.later = setTimeout(() => { st.later = 0; if (S.selected === ":hub:" + name) history(name); },
      Math.max(wait, 300));
    return;
  }
  st.loading = true;
  st.at = Date.now();
  const r = await act("history", { name });
  st.loading = false;
  if (!r.error) { st.rows = r.data.rows || []; st.error = ""; } else st.error = r.error;
  st.redraw?.();
}

const STATUS = () => ({ queued: ["⏳", t("web.bus.st_queued")], sent: ["↗", t("web.bus.st_sent")],
  delivered: ["✓", t("web.bus.st_delivered")], expired: ["✕", t("web.bus.st_expired")],
  refused: ["✕", t("web.bus.st_refused")], held: ["⏸", t("web.bus.st_held")] });

const OUTCOME = () => ({ passed: t("web.bus.oc_passed"), declined: t("web.bus.oc_declined"), muted: t("web.bus.oc_muted"),
  unverified: t("web.bus.oc_unverified"), held: t("web.bus.oc_held") });

// what the liaison did with a message of the other hub: [word, tone]
function inFate(r, outcomes) {
  if (r.status === "held") return [t("web.bus.st_held"), "warn"];
  if (r.status === "unverified") return [outcomes.unverified, "warn"];
  if (r.status === "muted" || r.outcome === "muted") return [outcomes.muted, "mute"];
  if (r.outcome === "passed") return [outcomes.passed + " " + (r.passed_to || []).join(", "), "ok"];
  if (r.outcome === "answered") return [t("web.bus.oc_answered"), "ok"];
  if (r.outcome === "declined") return [outcomes.declined, "bad"];
  if (r.outcome === "read") return [t("web.bus.oc_read"), "mute"];   // hub bus done: read, nothing to do
  return [t("web.bus.oc_pending"), "mute"];
}

function msgNode(r, c, statuses, outcomes) {
  const m = el("div", "bus-m " + r.way);
  m.dataset.id = r.id;
  const agent = r.agent ? r.agent + " · " + hubName(c) : hubName(c);
  const by = el("div", "bus-by");
  by.append(el("span", "bus-who", r.way === "in" ? t("web.bus.m_in", { from: agent, to: (r.passed_to || []).length ? r.passed_to.join(", ") : t("web.bus.liaison_name") })
      : t("web.bus.m_out", { from: r.source || t("web.bus.liaison_name"), to: agent })),
    el("span", "bus-when", when(r.ts)));
  m.append(by, el("div", "bus-t", r.text ?? t("web.bus.archived")));
  const foot = el("div", "bus-foot");
  if (r.way === "in") {
    const [word, tone] = inFate(r, outcomes);
    foot.append(chip(word, tone));
    if (r.why) foot.append(el("span", "hint", r.why));
    if (r.note) foot.append(el("span", "hint", t("web.bus.note", { note: r.note })));
    if (r.status === "unverified") foot.append(el("span", "hint", t("web.bus.held_unverified")));
  } else {
    const [mark, word] = statuses[r.status] || ["", r.status || ""];
    const tone = r.status === "delivered" ? "ok" : r.status === "expired" || r.status === "refused" ? "bad" : r.status === "held" ? "warn" : "mute";
    foot.append(chip([mark, word].filter(Boolean).join(" "), tone));
    if (r.auto) foot.append(el("span", "hint", t("web.bus.thread_auto", { head: r.head || r.thread })));   // the core chose the thread
  }
  m.append(foot);
  return m;
}

// the liaison's messages with this hub by thread: one block per thread, the latest message in it, the rest on a press
function blocks(rows) {
  const by = new Map(), out = [];
  for (const r of rows) {
    if (r.kind === "chat" || !r.thread) { out.push({ rows: [r], ts: r.ts, chat: r.kind === "chat" }); continue; }
    let g = by.get(r.thread);
    if (!g) { g = { thread: r.thread, rows: [], ts: 0 }; by.set(r.thread, g); out.push(g); }
    g.rows.push(r);
    g.ts = Math.max(g.ts, r.ts);
  }
  return out.sort((a, b) => a.ts - b.ts);
}

function draw(inner, st, c) {
  const open = st.open || (st.open = new Set());
  const sig = JSON.stringify(st.rows) + st.error + [...open].join();
  if (inner.dataset.sig === sig) return;
  inner.dataset.sig = sig;
  const f = inner.parentNode;
  const out = [];
  if (st.rows === null) out.push(el("p", "hint", t("web.bus.loading")));
  else if (!st.rows.length) {
    const e = el("div", "bus-empty");
    e.append(el("h3", "", t("web.bus.empty_title")), el("p", "hint", t("web.bus.empty_how", { label: c.label })));
    out.push(e);
  }
  let day = "";
  const statuses = STATUS(), outcomes = OUTCOME();
  for (const g of blocks(st.rows || [])) {
    const d = new Date(g.ts * 1000).toLocaleDateString(locale, { weekday: "long", day: "numeric", month: "long" });
    if (d !== day) { day = d; out.push(el("div", "day", d)); }
    const r = g.rows[0];
    if (g.chat) {
      const row = el("div", "bus-bub " + (r.way === "out" ? "me" : "you"));
      row.dataset.id = r.id;
      if (r.way === "in") row.append(el("div", "fr", t(r.reply ? "web.bus.reply_from" : "web.bus.from_other", { label: hubName(c) })));
      row.append(el("div", "bub" + (r.text == null ? " gone" : ""), r.text ?? t("web.bus.archived")));
      const [mark, word] = r.way === "out" ? statuses[r.status] || ["", r.status || ""] : ["", ""];
      const meta = el("div", "st " + (r.status || ""), [mark, word, when(r.ts)].filter(Boolean).join(" "));
      if (r.way === "out") meta.dataset.status = r.status || "";
      row.append(meta);
      out.push(row);
      continue;
    }
    const box = el("div", "bus-ex" + (g.rows.length > 1 ? " multi" : ""));
    if (g.thread) box.dataset.thread = g.thread;
    const shown = open.has(g.thread) ? g.rows : g.rows.slice(-1);
    if (g.rows.length > 1) {
      const head = el("button", "bus-ex-head");
      head.type = "button";
      head.setAttribute("aria-expanded", String(open.has(g.thread)));
      head.append(el("span", "bus-ex-n", t("web.bus.thread_n", { n: g.rows.length })),
        el("span", "bus-ex-toggle", open.has(g.thread) ? t("web.bus.thread_fold") : t("web.bus.thread_unfold", { n: g.rows.length - 1 })));
      head.addEventListener("click", () => { open.has(g.thread) ? open.delete(g.thread) : open.add(g.thread); draw(inner, st, c); });
      box.append(head);
    }
    for (const m of shown) box.append(msgNode(m, c, statuses, outcomes));
    out.push(box);
  }
  if (st.error) out.push(el("p", "bus-err", st.error));
  inner.replaceChildren(...out);
  if (!st.away && f) f.scrollTop = f.scrollHeight;
  st.sync?.();
}

export function refreshHubView() {
  for (const [, st] of chats) st.redraw?.();
}
