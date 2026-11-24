// Page plugins, API v1 (EXTENDING.md). Owner decision: an API and instructions so that vendor plugins can be developed
// on their own, with plugins able to add sections to settings, to the sidebar in various places, and to the top bar.
//
//   export default function register(hub) {
//     hub.addView({ id, title, subtitle?, icon?, full?, render(root, snap), show?(root), hide?(root), toolbar?(root, snap) })
//                                                                         own view: a sidebar item, #/x/<id>
//     hub.addSettings({ id, title, hint?, render(root, snap) })          own tab in "Settings"
//     hub.addSlot(slot, { id, order?, render(root, snap, ctx) })          an element at a place on the page (SLOTS below)
//   }
//   render is called on every core snapshot with the same root, so the element's state in it is not lost; for session
//   slots ctx is { session } (its row from the snapshot). hub.api is the plugin API version, hub.snapshot(),
//   hub.post(path, body), hub.go(id), hub.el(tag, cls, text). A plugin error does not break the page: its place is
//   left empty and the error is shown under "Plugins".
//   1.1 (the Flow view became a preinstalled plugin): show(root) — the view was opened, hide(root) — it was left
//   (start and stop animation, polling); icon — the item's icon: a name from the hub icons ("flow", "clock"...) or a
//   plugin file ("icon.svg"); full — the view fills the whole area without padding (canvas). hub.icon(name) is a hub
//   icon, hub.url(file) is the address of a plugin file, hub.addStyle(file) attaches its CSS.
//   1.2: hub.feed(since) — the plugin channel's posts after a number, hub.postFeed(text, reply_to) — the owner's post;
//   sessions write `hub post <plugin> "<text>" [--reply pN]` and read `hub feed <plugin>`; snap.feeds[<plugin>] is
//   the latest one.
//   1.5: slot sidebar.header — the sidebar header, in the "Hub sessions" row at the right edge (mascot, status icon).
//   1.6: slot session.aside — a panel to the right of the open session. If the plugin
//   leaves the slot empty there is no panel; title (a string or title(snap, ctx)) — the caption of the button in the
//   session header that reopens the panel (on a narrow screen it overlays the feed); ctx.close() closes the panel.
//   1.7: hub.input({ to, key?, placeholder?, hint?, prefix?(text), wake?(), onSent? }) — an input field like the
//   session chat's (attachments, Cmd+V, drag and drop, voice, draft, Enter/Shift+Enter); returns
//   { node, focus(), set({...}) } — keep the node between snapshots. The message goes to session `to` from the owner.
//   1.10: hub.addOrder(fn) — the order of rows in a session list group: fn(group, rows, snap) → rows (group: "agents",
//   "directions", "workers", and since 1.16 also the ids of the owner's own groups "g-..."; the owner's manual order
//   takes precedence); hub.redraw() — redraw the page (the plugin's own state changed, there is no snapshot).
//   1.12: hub.addOptions({ title, hint?, options: [{ key, label, hint?, on }] }) — a group of plugin checkboxes in
//   the "Settings" tab (after "About the hub"), so plugins and modules can add their own settings there; the value is
//   hub.option(key) (the choice is stored in the browser), when it changes the page is redrawn.
//   1.13: hub.addCommand({ name, args?, hint?, by?, run(args, ctx) }) — a command in the session input field: the owner
//   types "/name arguments", the page calls run itself, with no model and no session (ctx: { name, session } is the
//   open session). If run returns a string it is shown as a hint under the field; if it throws, the error text is shown
//   under the field and what was typed is returned.
//   Core command names (/compact, /model...) are taken; if two plugins use the same name the first one wins.
//   hub.addUnread((session, snap) → true|false) — the plugin decides "unread" for a session row (not a boolean — as the
//   core says); the snapshot row has owner_reply (the last result strictly to the owner) and seen (how far the owner
//   looked) for it. Example: hub-plugin-quiet-unread in the catalog.
//   1.14: ctx.open() in the session.aside slot — open the panel on its own (the plugin showed something on an event: a
//   file on click), even if it had been closed; on a narrow screen it overlays the feed.
//   1.28: input field transport — hub.capabilities.composerTransport === 1; hub.send(body) — an owner message,
//   POST /api/send with client_id, receipt {ok, receipt: accepted | refused | unknown, client_id, status, data?, error?};
//   hub.input({ ..., send(body) }) — the field sends through the plugin's own transport, input.draft() and
//   input.set({ text, files }) — read and restore the field's draft.
//   1.29: send receipt — hub.send waits for the core's decision: accepted (data.seq, data.id is the message id),
//   refused (the core refused, with a reason), queued (the core did not decide within 3 s) — the outcome comes later
//   from hub.receipt(session, client_id): accepted | pending | refused | unknown (null — the server did not answer);
//   hub.capabilities.receipt === 1.
//   1.30: hub.banner(text) — the hub's words in the page banner (the hub refused your request): a single line, up to
//   300 characters, text only, fades after 10 s, a click closes it; it does not cover the page's own banners
//   ("no connection", "updated"), and returns false where the banner is not visible (narrow screen, session list);
//   hub.capabilities.banner === 1.
//   1.30: slot sidebar.tiles — the top of the sidebar above the search row (section tiles): in the markup it comes
//   before the search, so Tab follows the visible order in Safari and Firefox; empty means no padding, the sidebar
//   looks as before.
//   1.31: dictation into a field — hub.dictate(field, { onText?(text, { inserted }), onError?(text, { retry?, download? }),
//   space? }) → a microphone button (a node; place it next to the field and keep it): recording as for a voice message,
//   the transcription goes into field (input, textarea, contenteditable) at the caret with an input event, nothing is
//   sent; if the field was rebuilt or became read-only during recognition, the text comes in onText with inserted: false
//   (a read-only or disabled field has no button); Space starts
//   recording only while the caret is in that field (space: false — the button only); no voice — the button is hidden;
//   not a field — null and an entry in Plugins. To check: hub.capabilities.dictate === 1.
//   1.21: a plugin from a core module (the web field in module.json) — listed in the snapshot's extensions with base
//   (/mod/<module>/web/) and the name "mod:<module>"; hub.url and hub.addStyle read from its folder. The snapshot has
//   mods (module state) and mod_versions.
//   1.28: navigating to a view item — hub.go(id, params): params is a plain object (values are strings, numbers,
//   true/false), they go into the address (#/x/<id>?plugin=teams&id=p7: a reload and the browser's "back" keep them),
//   the view receives them as strings in open(root, params, ctx) — on entry, on a new address and on a repeated
//   navigation to the same one; a view registered after the navigation gets them at registration. ctx: { from, close() }
//   — where the user came from (an address or null) and a way back there (if they did not come from a page, back goes
//   to the session list). With two navigations in a row the view receives only the last one.
//   1.31: hub.addSubgroups(fn) — subgroups inside a session list group (the "All" view): fn(group, rows, snap) →
//   [{ id, title, rows, open? }] or null (the group is not split); every row of the group goes into exactly one
//   subgroup, otherwise the split is not taken; the row order inside is as in the group (hub.addOrder, manual order),
//   empty subgroups are not shown. The core draws and folds the headers like its own groups; open: false means folded
//   until the owner opens it. Search opens everything. hub.groupFold(id, open?) — whether a group is open (true/false);
//   with open — fold or open it: the id of a group ("directions", "g-…"), of a "Now" section ("a:wait") or of a
//   subgroup ("directions/done").
//   hub.capabilities.subgroups === 1.
//   1.31: hub.input({ ..., leading() → node, leadingSig }) — the plugin's own node before the action in the field's
//   bottom row (kept across draft changes and snapshots, rebuilt only when leadingSig changes; leading: null removes
//   it); an exception or a non-element shows the field without it and an entry in Plugins. A field with it does not stack
//   (--composer-wrap). To check: hub.capabilities.composerLeading === 1.
//   1.32: hub.input(...).record() — starts a voice recording in the field as the microphone button does (call it from a
//   click of the plugin: the microphone is given only to a gesture); true — it started, false — voice is off or unavailable,
//   something is recording already or the field holds a draft. To check: hub.capabilities.inputRecord === 1.
//   1.31: toolbar(root, snap) in addView — the view's own controls in the page's top strip, only while the view is open;
//   root is the same node across snapshots and returns (an empty one takes no space). hub.capabilities.viewToolbar === 1.
//   A repeated addView with the same id from the same plugin keeps root, the toolbar root and the opened state.
//   1.35: slot session.header.start — the open session's header before the name, after the back-to-the-list button
//   where it is shown (a narrow screen; ctx.session): in the markup it comes before the title, so Tab follows the
//   visible order; empty — the header is as before.
//   1.36: hub.messageAt(node) — the message of a feed row for a plugin's own actions on it (a menu on the right click, a
//   long press): node is the row or anything inside it; → { session, kind, mine, target, text } or null (not a feed row).
//   target is what POST /api/react takes for the row (null — no reaction there), text is the message as written. The core
//   adds no menu and no handler: the plugin listens to the events itself. To check: hub.capabilities.messageAt === 1.
//   1.36: badge in addView({...}) — a positive whole number is shown as plain text at the right of the view's ☰ item (0, a
//   string, a negative or fractional number, NaN: nothing); to change it, register the view again. To check: hub.capabilities.viewBadge === 1.
//   1.37: hub.addOptions({ themes: ["dusk.json"], hint?, options }) — a group of a theme's own settings: shown in "Settings → Look"
//   under the row of that theme and only while it is chosen (a theme is a file from the plugin's manifest, as /api/themes
//   names it, <plugin>:<file>; another plugin's theme cannot be named); title is for older cores, which show it on "Look".
//   A call with [A, B] puts the group under A and B, replacing this plugin's earlier one there. hub.option(key) as before,
//   with no choice saved — the default of the chosen theme's group. To check: hub.capabilities.themeOptions === 1.
//   1.39: search in addView({...}) — { placeholder?, query(text) }: while the view is open the page's search field (the
//   one of the session list) is the view's: its placeholder, its text; typing calls query(text) at once, clearing (Esc,
//   ×) calls query(""); the session list is not filtered by it and Enter opens no session. Leaving the view gives the
//   field back to the session search with the text it had; coming back shows the view's own text again. hub.search({
//   placeholder?, text? }) — change the field from the open view (another tab) without a query call, also from render of
//   the view being opened (it acts on that view); → false when this plugin's view with search is neither drawn nor open.
//   On a narrow screen the list and the view are separate screens, so the field is not with the view: hub.searchField({
//   placeholder?, text?, onInput(text), mic? }) is the same search box for the view's own place (the magnifier, × while
//   there is text, the microphone unless mic: false) → { node, input, set({ text?, placeholder? }), focus() }; keep the
//   node between snapshots. Cmd+K goes to the shared field, or to a visible box of the open view (in it or its toolbar)
//   where the list is not on screen. To check: hub.capabilities.viewSearch === 1.
//   1.39: data-hub-message on a plugin's element with the text of one message (a letter, a to-do item): a triple click
//   selects all of it, a selected piece gets the selection bar with "Copy" (js/quotesel.js); no check needed.
import { el, icon, kit } from "./dom.js";
import { inputBox } from "./inputbox.js";
import { dictateButton, dictable, fieldMic } from "./dictate.js";
import { S, render } from "./state.js";
import { applyUnread, post, receiptOf, say, sendMessage } from "./api.js";
import { addCommand } from "./commands.js";
import { lang, locale, t } from "./i18n.js";
import { clock } from "./clock.js";
import { isOpen, setOpen } from "./fold.js";
import { messageOf } from "./feed.js";

export const SLOTS = {
  "sidebar.header": "the sidebar header: in the hub row, at the right edge",
  "sidebar.tiles": "the top of the sidebar, above the search row and earlier than it in the Tab order (1.30)",
  "sidebar.top": "below the search in the sidebar",
  "sidebar.nav": "in the sections menu (☰ in the hub row), next to \"Schedule\" and \"Flow\"; drawn even when the menu is closed",
  "sidebar.footer": "the sidebar footer",
  "sidebar.session": "at a session row in the list (ctx.session)",
  "topbar": "the top strip of the page",
  "session.header.start": "the header of the open session: before the name, after the back-to-the-list button (a narrow screen), and in the Tab order (ctx.session; 1.35)",
  "session.header": "the header of the open session (ctx.session)",
  "session.composer": "above the open session's input field (ctx.session)",
  "session.aside": "a panel to the right of the open session (ctx.session, ctx.close, ctx.open — 1.14); empty means no panel (1.6)",
};
const ID = /^[a-z0-9-]+$/;
const ROUTE = /^#\/x\/([a-z0-9-]+)(?:\?(.*))?$/;

// address of a plugin view: #/x/<id>[?k=v...] → {id, params}; null if it is not a view
export function routeOf(hash = location.hash) {
  const m = hash.match(ROUTE);
  return m ? { id: m[1], params: Object.fromEntries(new URLSearchParams(m[2] || "")) } : null;
}

// where the user came to a view from (1.28): remembered on navigation even if the view is not registered yet;
// navigation within the view (other params) keeps the previous origin address
const origins = new Map();   // view id -> the address it was entered from
addEventListener("hashchange", (e) => {
  const to = routeOf();
  if (!to) return;
  const from = new URL(e.oldURL).hash, back = routeOf(from)?.id;
  // going back to where the previous view was entered from (ctx.close(), the browser's "back") is not a new origin
  // address: otherwise two views would point to each other
  if (back !== to.id && !(back && origins.get(back) === location.hash)) origins.set(to.id, from || null);
});

// navigation params: a plain object with plain values; null/undefined means no value
function routeParams(params) {
  if (params == null) return "";
  if (typeof params !== "object" || Array.isArray(params) || ![Object.prototype, null].includes(Object.getPrototypeOf(params))) return null;
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null) continue;
    if (!["string", "number", "boolean"].includes(typeof v)) return null;
    q.append(k, String(v));
  }
  return q.toString();
}

const views = new Map();      // id -> {title, subtitle, render, root, ext}
const settings = new Map();   // id -> {title, hint, render, root, ext}
const slots = new Map();      // slot -> [{id, order, render, ext, roots: Map(key -> root)}]
const orders = [];            // [{ext, fn}] — row order in the list groups (1.10)
const subgroupers = [];       // [{ext, fn}] — subgroups inside the list groups (1.31)
const optionGroups = new Map();   // plugin -> {title, hint, options: [{key, label, hint, on}], n} — "Look", under the feed (1.12)
const themeGroups = new Map();    // theme id -> Map(plugin -> {hint, options, n}) — under that theme's row in "Look" (1.37)
let groupCount = 0;               // n: the order of registration — a key in several groups takes the latest one's default
let chosenTheme = () => "";       // the chosen theme's id — themes.js gives it (it imports this file, not the other way round)
export const setChosenTheme = (fn) => { chosenTheme = fn; };
let optionsQueued = false;        // one hub-options per turn of the page, after the calls (not inside a render that made them)
const loaded = new Set();
export const problems = new Map();   // plugin -> [text] — shown in "Settings" → "Plugins"

// view registry for plugins (1.28): only the descriptions of menu items — no nodes, functions or the registry itself
function publicViews() {
  return [
    { id: "settings", title: t("web.settings.tab.general"), icon: "account", owner: null },
    { id: "schedule", title: t("web.schedule.title"), icon: "clock", owner: null },
    ...[...views.values()].map((v) => ({ id: String(v.id), title: String(v.title || v.id),
      icon: fileIcon(v) ?? (typeof v.icon === "string" && /^[a-z-]+$/.test(v.icon) ? v.icon : "board"), owner: v.ext })),
  ].map((v, order) => ({ ...v, order }));
}

const viewListeners = new Set();
let viewsQueued = false, viewsSignature = null;
function viewsChanged() {
  if (!viewListeners.size || viewsQueued) return;
  viewsQueued = true;
  queueMicrotask(() => {
    viewsQueued = false;
    const signature = JSON.stringify(publicViews());
    if (signature === viewsSignature) return;
    viewsSignature = signature;
    for (const it of [...viewListeners]) {
      if (!viewListeners.has(it)) continue;
      try { it.fn(publicViews()); }
      catch (err) { problem(it.ext, t("web.ext.views_failed"), err); }
    }
  });
}

// addView({badge}) (1.36): the ☰ item's number — a positive whole number as plain text, "" (nothing shown) for anything else
const badgeText = (b) => Number.isSafeInteger(b) && b > 0 ? String(b) : "";

// a view icon given as a plugin file — its address (relative to base: /mod/<name>/web/ for a core module plugin), else null
function fileIcon(v) {
  return typeof v.icon === "string" && /\.(svg|png|webp)$/.test(v.icon) && !v.icon.includes("..")
    ? `${v.base || `/ext/${v.ext}/`}${v.icon}` : null;
}

function problem(name, text, err) {
  console.warn(t("web.ext.plugin_label", { name }) + `: ${text}`, err || "");
  problems.set(name, [...(problems.get(name) || []), text + (err ? `: ${err.message || err}` : "")].slice(-5));
}

// base — the address of the plugin's file folder: /ext/<name>/ for extensions, /mod/<name>/web/ for a core module plugin (1.21)
// builtIn — a part of the page on this API (registerBuiltIn): it names the core's own themes (core:clean) in addOptions
const api = (name, base = `/ext/${name}/`, builtIn = false) => ({
  api: S.snap?.plugin_api || "1.1",
  // what the page can do (1.28): name → version; no name means no capability (on an old core the field itself is absent)
  capabilities: Object.freeze({ clock: 1, composerTransport: 1, composerLeading: 1, views: 1, viewToolbar: 1, openSettings: 1, receipt: 1, banner: 1, subgroups: 1, dictate: 1, sidebar: 1, inputRecord: 1, messageAt: 1, viewBadge: 1, themeOptions: 1, viewSearch: 1 }),
  addView(v) {
    if (!ID.test(v.id || "")) return problem(name, t("web.ext.bad_view_id"));
    const previous = views.get(v.id);
    const own = previous?.ext === name ? previous : null;
    const next = { ...v, ext: name, base, root: own?.root || null,
      toolbarRoot: own?.toolbarRoot || null, opened: own?.opened };
    views.set(v.id, next);
    if (own?.search?.placeholder !== v.search?.placeholder) viewHints.delete(v.id);   // the newer placeholder wins
    if (!own || searching(own) !== searching(v)) viewTexts.delete(v.id);   // another view now: no text it never got
    if (own && shown === own) { shown = next; syncSearch(); }
    viewsChanged();
    // the ☰ item's number changed (badge is not in views(), so no listener fires): compared as shown, so NaN or a new object each time does not re-render
    const numberChanged = previous && badgeText(previous.badge) !== badgeText(v.badge);
    if (numberChanged || routeOf()?.id === v.id) queueMicrotask(render);   // already navigated to it (1.28) — open once the plugin finishes registering
  },
  addSettings(s) {
    if (!ID.test(s.id || "")) return problem(name, t("web.ext.bad_settings_id"));
    settings.set(s.id, { ...s, ext: name, root: null });
  },
  addSlot(slot, item) {
    if (!SLOTS[slot]) return problem(name, t("web.ext.no_slot", { slot, slots: Object.keys(SLOTS).join(", ") }));
    if (!ID.test(item?.id || "") || typeof item.render !== "function") return problem(name, t("web.ext.slot_needs", { slot }));
    const list = (slots.get(slot) || []).filter((x) => !(x.ext === name && x.id === item.id));
    list.push({ ...item, ext: name, order: Number(item.order) || 0, roots: new Map() });
    slots.set(slot, list.sort((a, b) => a.order - b.order));
  },
  addOrder(fn) {
    if (typeof fn !== "function") return problem(name, t("web.ext.order_needs"));
    orders.push({ ext: name, fn });
  },
  addSubgroups(fn) {
    if (typeof fn !== "function") return problem(name, t("web.ext.subgroups_needs"));
    subgroupers.push({ ext: name, fn });
  },
  // whether a list group is open; with open — fold or open it (as its header does), the list is redrawn
  groupFold(id, open) {
    if (typeof id !== "string" || !id) return null;
    if (typeof open === "boolean" && open !== isOpen(id)) { setOpen(id, open); redraw(); }
    return isOpen(id);
  },
  addUnread(fn) {
    if (typeof fn !== "function") return problem(name, t("web.ext.unread_needs"));
    S.unread.push({ ext: name, fn });
    redraw();
  },
  addOptions(g) {
    const options = (g?.options || []).filter((o) => ID.test(o?.key || "") && typeof o.label === "string");
    const themed = g?.themes !== undefined;
    if (themed && !(Array.isArray(g.themes) && g.themes.length && g.themes.every((f) => typeof f === "string" && f) && options.length))
      return problem(name, t("web.ext.options_themes"));
    if (!themed && (!g?.title || !options.length)) return problem(name, t("web.ext.options_needs"));
    const group = { hint: g.hint ? String(g.hint) : "",
      options: options.map((o) => ({ key: o.key, label: o.label, hint: o.hint ? String(o.hint) : "", on: !!o.on })) };
    group.sig = JSON.stringify([themed ? "" : String(g.title), group.hint, group.options]);
    const same = (old) => old?.sig === group.sig;
    // a theme of the plugin by its file, as /api/themes names it; the core's own part names the core's themes as they are
    const ids = themed ? g.themes.map((f) => (builtIn ? f : `${name}:${f}`)) : [];
    // the same group again (a plugin that declares it on every render) changes nothing and asks for no redraw
    if (themed ? ids.every((id) => same(themeGroups.get(id)?.get(name))) : same(optionGroups.get(name))) return;
    group.n = ++groupCount;
    if (!themed) optionGroups.set(name, { title: String(g.title), ...group });
    else for (const id of ids) themeGroups.set(id, new Map(themeGroups.get(id)).set(name, group));
    if (optionsQueued) return;
    optionsQueued = true;
    queueMicrotask(() => { optionsQueued = false; dispatchEvent(new Event("hub-options")); });   // an open "Look" shows it
  },
  option: (key) => optionValue(name, key),
  addCommand(cmd) {
    const bad = addCommand(name, cmd);
    if (bad) problem(name, bad);
  },
  redraw,
  snapshot: () => S.snap,
  views: publicViews,
  on(event, fn) {
    if (event === "sidebar" && typeof fn === "function") {
      const changed = (e) => { try { fn({ ...e.detail }); } catch (err) { problem(name, t("web.ext.sidebar_failed"), err); } };
      addEventListener("hub-sidebar-changed", changed);
      return () => removeEventListener("hub-sidebar-changed", changed);
    }
    if (event !== "views" || typeof fn !== "function") return () => {};
    if (!viewListeners.size) viewsSignature = JSON.stringify(publicViews());
    const it = { ext: name, fn };
    viewListeners.add(it);
    return () => viewListeners.delete(it);
  },
  openSettings: (tab = "about") => !dispatchEvent(new CustomEvent("hub-open-settings", { detail: tab, cancelable: true })),
  sidebar: (options = {}) => {
    const detail = { open: options?.open };
    dispatchEvent(new CustomEvent("hub-sidebar-request", { detail }));
    return detail.result || null;
  },
  post,
  send: (body) => sendMessage({ ...body }),
  receipt: (session, clientId) => receiptOf(session, clientId),
  banner: (text) => say(text),
  messageAt: (node) => messageOf(node),
  input: (opts) => inputBox({ ...opts, key: `${name}:${opts.key || opts.to}`,
    fault: (err) => problem(name, t("web.ext.leading_failed"), err) }),
  dictate: (field, opts) => (dictable(field)
    ? dictateButton(field, opts || {}, (what, err) => problem(name, t("web.ext.dictate_failed", { what }), err))
    : problem(name, t("web.ext.dictate_needs")) || null),
  search: (opts) => setSearch(name, opts),
  searchField: (opts) => searchBox(name, opts),
  go: (id, params) => {
    const q = routeParams(params);
    if (q == null) return problem(name, t("web.ext.go_params", { id }));
    const hash = `#/x/${id}` + (q ? `?${q}` : "");
    if (location.hash !== hash) { location.hash = hash; return; }
    const v = views.get(id);   // same address: there will be no hashchange — the open view gets the item once more
    if (v && shown === v) opened(v, hash);
  },
  el,
  icon,
  // 1.25: the page language (code: ru, en, de...; xx is a test one), the browser's date and number locale, the core
  // catalog strings (i18n/<lang>.json — a plugin keeps its own strings and picks them by hub.lang)
  lang,
  locale,
  t,
  clock: (ts, date) => clock(ts, locale, date),   // 1.39: the page's one clock
  url: (file) => `${base}${file}`,
  // 1.2: the plugin's channel — posts to the hub journal (hub post / hub feed in sessions); the channel name is the
  // plugin name without the author
  feed: async (since = 0) => (await (await fetch(`/api/feed?plugin=${encodeURIComponent(name.split(":").pop())}&since=${since}`)).json()).posts || [],
  // posts by number (p<seq>), also those past the 500-post window (the hub reads them back from the journal), in the order
  // asked; API 1.32, only if `features` has "feed.post_by_id"; the unknown ones are skipped. The server takes up to 20
  // numbers a request and refuses a malformed one: the list goes in parts of 20, malformed numbers are not sent
  feedPosts: async (ids) => {
    const want = [...new Set((ids || []).filter((id) => typeof id === "string" && /^p[1-9][0-9]{0,14}$/.test(id)))];
    const out = [];
    for (let i = 0; i < want.length; i += 20) {   // with no post= the server answers with the whole window: never empty
      const part = want.slice(i, i + 20);
      const r = await fetch(`/api/feed?plugin=${encodeURIComponent(name.split(":").pop())}&${part.map((id) => `post=${encodeURIComponent(id)}`).join("&")}`);
      // a page server not yet restarted after the update ignores post= and answers with the window: only the asked ones
      if (r.ok) out.push(...((await r.json()).posts || []).filter((p) => part.includes(p.id)));
    }
    return out;
  },
  postFeed: (text, replyTo) => post("/api/feed/post", { plugin: name.split(":").pop(), text, ...(replyTo ? { reply_to: replyTo } : {}) }),
  addStyle: (file, opts) => attachStyle(name, `${base}${file}`, opts),
});

// a sheet a plugin's code turned off or narrowed by media (a look the person switched off) is kept in this browser for
// the first frame of the next load (js/first-paint.js): the sheets of the css field are in <head> from the first byte,
// the code runs only after the page is drawn. Only what the plugin asks for through hub.addStyle(file, { on }) (1.39),
// one entry per sheet (href → media), so a plugin that is absent for a load keeps its entry; a sheet the plugin did not
// ask about by the end of its register() goes back to the state of the list — the page keeps no state of a plugin that
// the plugin's code no longer sets (a version without the switch, a switch the code only sets when it is off)
const SHEET_MEDIA = "hub.sheetMedia";
const sheetsAsked = new Set();   // the hrefs hub.addStyle(file, { on }) spoke for in this load
function sheetRecord() { try { return JSON.parse(localStorage.getItem(SHEET_MEDIA) || "null") || {}; } catch { return {}; } }
function saveSheetRecord(rec) {
  try {
    if (Object.keys(rec).length) localStorage.setItem(SHEET_MEDIA, JSON.stringify(rec));
    else localStorage.removeItem(SHEET_MEDIA);
  } catch { /* no storage: the next load starts with the sheets as listed */ }
}
function setSheet(link, on) {
  const href = link.getAttribute("href"), rec = sheetRecord();
  sheetsAsked.add(href);
  if (on) { link.removeAttribute("media"); delete rec[href]; } else { link.media = "not all"; rec[href] = "not all"; }
  saveSheetRecord(rec);
}
// after a plugin's register(): a listed sheet in the record it did not speak for goes out of the record, and its media goes
// off the link if it is still the record's (the "off" was the page's from the first frame, not the code's); a media the code
// set on the link by hand — with no record, or over the record's value — is its own and stays
function settleSheets(name) {
  const rec = sheetRecord();
  let changed = false;
  for (const l of document.querySelectorAll(`link[rel=stylesheet][data-plugin="${CSS.escape(name)}"]`)) {
    const href = l.getAttribute("href");
    if (sheetsAsked.has(href) || !(href in rec)) continue;
    if (l.getAttribute("media") === rec[href]) l.removeAttribute("media");
    delete rec[href]; changed = true;
  }
  if (changed) saveSheetRecord(rec);
}

// a plugin's stylesheet in <head>, once: the same address as the page's own <link> for a "css" entry of the plugin's record;
// { on: false } turns the sheet off (media "not all") and keeps that for the next load, { on: true } turns it back on
function attachStyle(name, href, opts) {
  const full = new URL(href, document.baseURI).href;   // "./web/x.css" and "web/x.css" are the same sheet
  let l = [...document.querySelectorAll("link[rel=stylesheet]")].find((x) => x.href === full);
  if (!l) {
    l = document.createElement("link");
    l.rel = "stylesheet"; l.href = href; l.dataset.plugin = name;
    document.head.append(l);
  }
  if (opts && typeof opts.on === "boolean") setSheet(l, opts.on);
}

// plugin checkboxes (1.12): the choice lives in the browser, key hub.opt.<plugin>; no choice means the default from addOptions
const optKey = (ext) => `hub.opt.${ext}`;
function optionsOf(ext) { try { return JSON.parse(localStorage.getItem(optKey(ext)) || "{}") || {}; } catch { return {}; } }
export function optionValue(ext, key) {
  const v = optionsOf(ext)[key];
  if (typeof v === "boolean") return v;
  // no choice yet: the default of the chosen theme's group, else of the plugin's latest group with this key
  const has = (g) => g?.options.some((o) => o.key === key);
  const all = [...new Set([optionGroups.get(ext), ...[...themeGroups.values()].map((m) => m.get(ext))])].filter(has);
  const mine = all.length > 1 ? themeGroups.get(chosenTheme())?.get(ext) : null;   // the theme is read only when it decides
  const g = has(mine) ? mine : all.sort((a, b) => b.n - a.n)[0];
  return !!g?.options.find((o) => o.key === key).on;
}
export function setOption(ext, key, on) {
  try { localStorage.setItem(optKey(ext), JSON.stringify({ ...optionsOf(ext), [key]: on })); } catch { /* no storage */ }
  redraw();
}

// the plugin's own state changed (hub.redraw, a checkbox, a new addUnread) — recompute the plugins' "new" first, then the list
function redraw() {
  applyUnread();
  dispatchEvent(new Event("hub-redraw"));
}
export const pluginOptions = () => [...optionGroups].map(([ext, g]) => ({ ext, ...g }));
// the groups of a theme's own settings (1.37), in the order they were declared
export const themeOptions = (id) => [...(themeGroups.get(id) || [])].map(([ext, g]) => ({ ext, ...g })).sort((a, b) => a.n - b.n);

// a part of the page written on this API — the Clean theme (clean/clean.js): main.js registers it before the first render,
// not by the snapshot's list; it is not in "Plugins" and is not turned off there (its switch is the theme, js/themes.js)
export function registerBuiltIn(name, base, register) {
  try { register(api(name, base, true)); } catch (err) { problem(name, t("web.ext.load_failed"), err); }
}

// catalog plugins that became part of the page (web/server.py IN_CORE): the server no longer lists them, and a server that
// has not restarted since the update is not asked to — two copies of Clean would change the page twice
const IN_CORE = new Set(["kalimullin:clean", "clean"]);

let loading = 0;
export const extLoading = () => loading > 0 || !S.snap;   // plugins are still loading — too early to show "No such view"

export async function loadExtensions() {
  // A render during an import must not start a second registration pass. Only the active call returns fresh and
  // requests a redraw; that redraw picks up extensions announced while it was waiting.
  if (loading) return false;
  const todo = (S.snap.extensions || []).filter((e) => !loaded.has(e.name) && !IN_CORE.has(e.name));
  // every plugin's code is asked for at once, not after the previous one has registered: one after another, the last
  // of twenty was fetched only after the nineteen before it had run. A preload only fetches; the code still runs one
  // plugin after another
  for (const e of todo) preload(`${e.base || `/ext/${e.name}/`}${e.main}`);
  let fresh = false;
  for (const e of todo) {
    if (loaded.has(e.name)) continue;
    loaded.add(e.name);
    fresh = true;
    loading += 1;
    try {
      const base = e.base || `/ext/${e.name}/`;   // a core module plugin (1.21) — from the module's folder
      // the "css" files (1.37): the server already put them into the page it served; a plugin that came while the page
      // was open (or a page served by a server from before the field) takes them here, before its code runs
      for (const f of Array.isArray(e.css) ? e.css : []) if (typeof f === "string") attachStyle(e.name, `${base}${f}`);
      const mod = await import(`${base}${e.main}`);
      const out = (mod.default || mod.register)(api(e.name, base));
      // the sheets settle once register() is done — after its promise for an async one (its asks after an await count),
      // without holding the plugins after it; an async register() that threw is a failed load like a sync one (shown in
      // "Settings" → "Plugins", not a rejection nobody heard) and keeps its record as it was
      Promise.resolve(out).then(() => settleSheets(e.name), (err) => problem(e.name, t("web.ext.load_failed"), err));
    } catch (err) { problem(e.name, t("web.ext.load_failed"), err); }
    loading -= 1;
  }
  return fresh;
}

function preload(href) {
  if (document.querySelector(`link[rel="modulepreload"][href="${CSS.escape(href)}"]`)) return;
  const link = document.createElement("link");
  link.rel = "modulepreload";
  link.href = href;
  document.head.append(link);
}

function draw(name, what, fn) {
  try { fn(); } catch (err) { problem(name, what, err); }
}

// the row order of a session list group — plugins apply it in turn (1.10); if a plugin threw or returned different
// rows, its order is ignored: it cannot lose, duplicate or add rows
export function ordered(group, rows) {
  let out = rows;
  for (const o of orders) {
    draw(o.ext, t("web.ext.order_failed", { group }), () => {
      const got = o.fn(group, [...out], S.snap);
      if (Array.isArray(got) && got.length === out.length && new Set(got).size === got.length
          && got.every((r) => out.includes(r))) out = got;   // the same rows, no duplicates
    });
  }
  return out;
}

// subgroups of a session list group (1.31) — from the first plugin that split it properly: every row in exactly one
// subgroup; otherwise its split is not taken (as with the order). null means a group without subgroups
export function subgroups(group, rows) {
  for (const o of subgroupers) {
    let out = null;
    draw(o.ext, t("web.ext.subgroups_failed", { group }), () => {
      const got = o.fn(group, [...rows], S.snap);
      if (!Array.isArray(got)) return;
      const ids = new Set(), seen = new Set();
      const fine = got.every((g) => g && ID.test(g.id || "") && !ids.has(g.id) && ids.add(g.id)
        && typeof g.title === "string" && g.title && Array.isArray(g.rows)
        && g.rows.every((r) => rows.includes(r) && !seen.has(r) && seen.add(r)));
      if (fine && seen.size === rows.length) out = got.map((g) => ({ id: g.id, title: g.title, open: g.open !== false,
        rows: rows.filter((r) => g.rows.includes(r)) })).filter((g) => g.rows.length);
    });
    if (out) return out;
  }
  return null;
}

// nodes of a slot: each element has its own root (and each session has one for session slots); empty means an empty list
export function slotNodes(slot, ctx = {}) {
  const key = ctx.session?.name || "";
  return (slots.get(slot) || []).map((it) => {
    let root = it.roots.get(key);
    if (!root) {
      root = el(slot === "sidebar.session" ? "span" : "div", `slot slot-${slot.replaceAll(".", "-")}`);   // session.header.start -> slot-session-header-start
      root.dataset.plugin = it.ext;
      it.roots.set(key, root);
    }
    draw(it.ext, t("web.ext.slot_failed", { slot }), () => {
      it.render(root, S.snap, ctx);
      if (it.title) root.dataset.title = typeof it.title === "function" ? it.title(S.snap, ctx) || "" : it.title;
    });
    return root;
  });
}

// view items in the ☰ menu — the same nodes between snapshots (keyed by view id): a recreated item under the mouse
// restarted its hover animation with a jerk; only what changed is updated in place
const navNodes = new Map();   // view id -> { b, g, nm, lb, icon }
export function extNavs() {
  for (const id of navNodes.keys()) if (!views.has(id)) navNodes.delete(id);
  return [...views.values()].map((v) => {
    let n = navNodes.get(v.id);
    if (!n) {
      n = { b: el("button", "srow nav"), g: el("span", "glyph"), nm: el("span", "nm"), lb: el("span", "lb"), c: el("span", "nav-count"), icon: null };
      n.b.type = "button";
      const txt = el("span", "txt");
      txt.append(n.nm, n.lb);
      n.b.append(n.g, txt, n.c);
      n.b.addEventListener("click", () => { location.hash = `#/x/${v.id}`; });
      navNodes.set(v.id, n);
    }
    const name = v.title || v.id, sub = v.subtitle || t("web.ext.plugin_label", { name: v.ext });
    setAttr(n.b, "aria-current", String(S.selected === `:x:${v.id}`));
    setAttr(n.b, "title", `${name}\n${sub}`);   // the full description goes into the tooltip
    if (n.nm.textContent !== name) n.nm.textContent = name;
    if (n.lb.textContent !== sub) n.lb.textContent = sub;
    const num = badgeText(v.badge);
    if (n.c.textContent !== num) n.c.textContent = num;
    n.c.hidden = !num;
    if (num) setAttr(n.b, "aria-label", `${name} ${sub}, ${num}`); else n.b.removeAttribute("aria-label");
    const sig = fileIcon(v) || navIconName(v) + (kit.icons[navIconName(v)] || "");   // the theme has its own icon — and it changes the node
    if (n.icon !== sig) { n.icon = sig; n.g.replaceChildren(navIcon(v)); }
    return n.b;
  });
}
const setAttr = (n, k, v) => { if (n.getAttribute(k) !== v) n.setAttribute(k, v); };

const navIconName = (v) => (/^[a-z-]+$/.test(v.icon || "") ? v.icon : "board");
function navIcon(v) {
  const file = fileIcon(v);
  if (file) {
    const m = el("span", "icon ext-icon");
    m.style.setProperty("--src", `url("${file}")`);
    return m;
  }
  return icon(navIconName(v));
}

let shown = null;   // the open plugin view: it gets show on entry and hide on leaving
// the frames queued on entry act on the view's current registration while it is still open (a plugin may register the
// same view again from render or show — addView keeps its root and moves shown to it); left before the frame — nothing
const current = (v) => (shown?.id === v.id && shown.root === v.root ? shown : null);
// The view owns its controls; the host owns their shared topbar placement. Keep
// the root across snapshots and metadata updates so search text/caret survive.
export function extToolbar(id) {
  const v = views.get(id);
  if (typeof v?.toolbar !== "function") return null;
  if (!v.toolbarRoot) v.toolbarRoot = el("div", "view-toolbar");
  draw(v.ext, t("web.ext.view_failed", { id }), () => v.toolbar(v.toolbarRoot, S.snap));
  return v.toolbarRoot.childNodes.length ? v.toolbarRoot : null;
}

export function extView(id) {
  let v = views.get(id);
  if (!v) { extHide(); return null; }   // "No view" on screen: the last view lets go of the field (1.39)
  if (!v.root) v.root = el("div", "ext-view" + (v.full ? " full" : ""));
  v.root.classList.toggle("full", !!v.full);
  rendering = v;   // hub.search from render acts on this view, though it is not the shown one yet
  try { draw(v.ext, t("web.ext.view_failed", { id }), () => v.render(v.root, S.snap)); } finally { rendering = null; }
  if (views.get(id)?.root === v.root) v = views.get(id);   // render registered the view again: enter the new registration
  const entered = shown !== v;
  if (entered) {
    extHide();
    shown = v;
    syncSearch();
    if (v.show) requestAnimationFrame(() => { const cur = current(v);   // already in the page
      if (cur?.show) draw(cur.ext, t("web.ext.view_show_failed", { id }), () => cur.show(cur.root)); });
  }
  if (entered || v.opened !== location.hash) opened(v, location.hash);
  return v.root;
}

// address params go to the view (1.28), after show, when it is already in the page; if the address changed while
// waiting for the frame, skip this one
function opened(v, hash) {
  v.opened = hash;
  if (!v.open) return;
  requestAnimationFrame(() => {
    const cur = current(v);
    if (!cur?.open || location.hash !== hash) return;
    const from = origins.get(cur.id) ?? null;
    const ctx = { from, close: () => { location.hash = origins.get(cur.id) || "#/sessions"; } };
    draw(cur.ext, t("web.ext.view_open_failed", { id: cur.id }), () => cur.open(cur.root, routeOf(hash)?.params || {}, ctx));
  });
}

export function extHide() {
  const v = shown;
  shown = null;
  syncSearch();
  if (v?.hide) draw(v.ext, t("web.ext.view_hide_failed", { id: v.id }), () => v.hide(v.root));
}

// The page's search field and the open view (1.39): a view with search holds the field while it is open. The page keeps
// one text for the session search and one per view, so leaving and coming back loses neither.
let holder = null;   // id of the view holding the field
let sessionText = "";   // the session search while a view holds the field; its placeholder waits in data-session-hint
// (a theme turned off meanwhile puts its own original there: clean.js)
const viewTexts = new Map(), viewHints = new Map();   // view id → its text, the placeholder hub.search gave
const searching = (v) => typeof v?.search?.query === "function";
// an empty placeholder is the default one: the × of the field hides by :placeholder-shown, which an empty one never is
const hintOf = (v) => viewHints.get(v.id) || (typeof v.search.placeholder === "string" && v.search.placeholder
  ? v.search.placeholder : t("web.clean.find"));
let rendering = null;   // the view whose render runs now
export function syncSearch() {
  const f = document.getElementById("filter");
  if (!f) return;
  const v = searching(shown) ? shown : null, id = v?.id ?? null;
  if (id !== holder) {
    if (holder) viewTexts.set(holder, f.value); else { sessionText = f.value; f.dataset.sessionHint = f.placeholder; }
    f.value = id ? viewTexts.get(id) || "" : sessionText;
    holder = id;
    if (!id) {   // as a theme had it
      delete f.dataset.view;
      f.placeholder = f.dataset.sessionHint || t("web.sidebar.find");
      delete f.dataset.sessionHint;
    }
  }
  if (!v) return;
  f.dataset.view = id;
  if (f.placeholder !== hintOf(v)) f.placeholder = hintOf(v);   // also after the language changed (data-i18n-placeholder)
}
// what the session list filters by: the field; nothing while a view holds it — the field shows the view's text, and a
// list cut by a hidden query would look broken (its text comes back with the field)
export const sessionQuery = () => (holder ? "" : document.getElementById("filter")?.value || "");
export const searchHeld = () => !!holder;
// a session chosen from the list ends the session search; while a view holds the field, that search is the hidden text
// the field gets back when the view lets go of it (sidebar.js endSearch)
export function clearSessionQuery() { sessionText = ""; }
// the field's text went to the view holding it (typing, Esc, ×)
export function viewQuery(text) {
  const v = holder && shown?.id === holder ? shown : null;
  if (searching(v)) draw(v.ext, t("web.ext.view_failed", { id: v.id }), () => v.search.query(text));
}
// from render: the view being drawn (it becomes the shown one right after); otherwise the open view holding the field.
// The view not holding the field yet keeps both for when it takes it
function setSearch(name, opts) {
  const v = rendering ? views.get(rendering.id) : holder && shown?.id === holder ? shown : null;
  if (!searching(v) || v.ext !== name || !opts || typeof opts !== "object") return false;
  const f = document.getElementById("filter"), held = holder === v.id && !!f;
  if (typeof opts.placeholder === "string") viewHints.set(v.id, opts.placeholder);
  if (typeof opts.text === "string") { if (held) f.value = opts.text; else viewTexts.set(v.id, opts.text); }
  if (held && f.placeholder !== hintOf(v)) f.placeholder = hintOf(v);
  return true;
}
// Cmd+K: the shared field; where it is not on screen (a narrow screen with a view open), a visible box of the view
// (in the view or in its toolbar). Looked up on the screen each time: a plugin keeps its box between snapshots, and the
// box comes back with the view after a round trip through a session
// (a folded list is visibility: hidden — it has boxes on the page, yet it is not on screen)
const onScreen = (n) => (n.checkVisibility ? n.checkVisibility({ visibilityProperty: true }) : !!n.getClientRects().length);
export function focusSearch() {
  const f = document.getElementById("filter");
  const own = shown && !onScreen(f) ? [shown.root, shown.toolbarRoot]
    .flatMap((r) => (r?.isConnected ? [...r.querySelectorAll(".view-search input")] : []))
    .find(onScreen) : null;
  (own || f).focus();
  (own || f).select();
}
// the session list's search box for a plugin's own place: the same classes, so the look and the themes style it alike
function searchBox(name, opts) {
  if (!opts || typeof opts !== "object") opts = {};
  const box = el("label", "search view-search");
  const input = el("input");
  input.type = "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  const said = () => { if (typeof opts.onInput === "function")
    draw(name, t("web.ext.search_failed"), () => opts.onInput(input.value)); };
  const set = (o = {}) => {   // an empty placeholder is the default one (the × hides by :placeholder-shown)
    if (typeof o.placeholder === "string") input.placeholder = o.placeholder || t("web.clean.find");
    if (typeof o.text === "string") input.value = o.text;
  };
  set({ placeholder: typeof opts.placeholder === "string" ? opts.placeholder : "", text: opts.text ?? "" });
  input.addEventListener("input", said);
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !input.value) return;
    e.preventDefault();
    input.value = "";
    said();
  });
  const x = el("button", "search-x");
  x.type = "button";
  x.append(icon("close"));
  x.title = t("web.clean.clear_search");
  x.setAttribute("aria-label", x.title);
  x.addEventListener("click", (e) => { e.preventDefault(); input.value = ""; said(); input.focus(); });
  const end = el("span", "search-end");
  end.append(x);
  if (opts.mic !== false && dictable(input)) end.append(fieldMic(input, { space: false }));
  box.append(icon("search"), input, end);
  return { node: box, input, set, focus: () => input.focus() };
}

// "Settings" tabs from plugins: id, title, node (the same between snapshots)
export const hasView = (id) => views.has(id);   // the plugin's view exists — without drawing it
export const hasSettingsTab = (id) => settings.has(id);   // the plugin's tab exists — without drawing it
export function settingsTabs() {
  return [...settings.values()].map((s) => {
    if (!s.root) {
      s.root = el("section", "settings-sec");
      s.body = el("div");
      s.root.append(el("h3", "day", s.title || s.id), ...(s.hint ? [el("p", "hint", s.hint)] : []), s.body);
    }
    draw(s.ext, t("web.ext.settings_failed", { id: s.id }), () => s.render(s.body, S.snap));
    return { id: `x:${s.id}`, title: s.title || s.id, node: s.root };
  });
}
