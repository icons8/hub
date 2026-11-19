// Settings and service information (browsers are tucked away into settings, on a page for extra data).
// The side list holds only sessions and the schedule; everything else is here. As tabs: "About" (version, what's
// new, and what is not set up yet), "General" (the hub's main decisions + a single "Advanced"), "Modules" (what each gives, its toggle and
// settings), "Accounts", "Plugins", "Look" (everything kept in the browser: theme, size, feed), then the plugin
// tabs (hub.addSettings). Tabs with their own state (a search field, plugin tabs)
// are built once and not reset by new core snapshots.
import { aboutSection, localEditsBand } from "./updates.js";
import { el, icon } from "./dom.js";
import { S, render, BRIEF, setBrief, setShortPaths, setFeedAtEnd, foldOn } from "./state.js";
for (const e of ["hub-modules", "hub-plugins", "hub-about"]) window.addEventListener(e, () => { if (S.selected === ":settings") render(); });
import { toList } from "./dialogs.js";
import { settingRow, agentRow, tickRow } from "./settingrow.js";
import { browsersDown } from "./browsers.js";
import { modulesSection } from "./modules.js";
import { pluginsSection } from "./plugins.js";
import { accountsSection } from "./accounts.js";
import { secretsSection, refreshSecrets } from "./secrets.js";
import { appSetupSection, setupOn } from "./setup.js";
import { codexSection } from "./codex.js";
import { instancesSection, refreshInstances } from "./instances.js";
import { themesSection } from "./themes.js";
import { hubsSection, refreshHubs } from "./hubs.js";
import { settingsTabs, hasSettingsTab, hasView, pluginOptions, optionValue, setOption } from "./ext.js";
import { t } from "./i18n.js";
import { keepOpen } from "./keepopen.js";

const TAB = "hub.settingsTab";
const keep = {};   // id -> tab node that is not rebuilt with a snapshot
let pendingTabFocus = null;   // the focused tab — across several re-renders until the next frame
// built-in tabs are separate from plugin tabs: settingsTabs() draws those (plugin render)
const builtinTabs = () => [
  // no «Setup» tab: what is not set up yet (modules/setup) is a card on top of "About" — a page server without it (older,
  // before its restart) has none
  { id: "about", title: t("web.settings.tab.about"), make: () => (setupOn() ? withSetup(aboutSection()) : aboutSection()) },
  { id: "general", title: t("web.settings.tab.main"), make: generalSection, keep: true, refresh: refreshGeneral },   // right after "About"
  { id: "modules", title: t("web.settings.tab.modules") + (browsersDown() ? " ·" : ""), make: modulesSection },
  // session login is handled by a module that did not answer (snapshot accounts: null) — no tab
  // a hub entirely on Codex (hub.json "host": "codex"): the Codex sign-in instead of Claude's accounts
  ...(S.snap?.codex ? [{ id: "codex", title: "Codex", make: codexSection }]
    : S.snap?.accounts === null ? [] : [{ id: "accounts", title: t("web.settings.tab.accounts"), make: accountsSection }]),
  // the hub's vault (spec d2345345): here unless the owner turned it off (secrets.enabled: false in hub.json)
  ...((S.snap.settings || []).find((r) => r.key === "secrets.enabled")?.value === false ? []
    : [{ id: "secrets", title: t("web.settings.tab.secrets"), make: secretsSection, keep: true, refresh: refreshSecrets }]),
  // the hub bus (spec hubbus/ux §3.2): always here — without it the feature cannot be found
  { id: "hubs", title: t("web.settings.tab.hubs"), make: hubsSection, keep: true, refresh: refreshHubs },
  { id: "plugins", title: t("web.settings.tab.plugins") + ((S.snap.plugins || []).some((p) => p.warning && !p.disabled) ? " ·" : ""), make: pluginsSection },
  { id: "theme", title: t("web.settings.tab.look"), make: lookSection, keep: true, refresh: refreshLook },   // theme, size, feed — all "in this browser"
];
const tabs = () => [...builtinTabs(), ...settingsTabs()];
// the browser and the check come first, before the facts and «What's new»: on the app they are what the person comes here for
const withSetup = (about) => { about.prepend(appSetupSection()); return about; };

// a plugin's hub.openSettings(tab) (1.28): the tab choice and memory live here, as with a click
addEventListener("hub-open-settings", (e) => {
  const id = String(e.detail);
  if (id === "x:usage" && hasView("usage")) {   // "Usage" left the tabs for a ☰ view next to "Flow": the old tab address leads there
    e.preventDefault();
    location.hash = "#/x/usage";
    return;
  }
  // whether the tab exists — without tabs(): that calls render() of every plugin's tabs
  if (!S.snap || !(builtinTabs().some((x) => x.id === id) || id.startsWith("x:") && hasSettingsTab(id.slice(2)))) return;
  localStorage.setItem(TAB, id);
  e.preventDefault();
  if (location.hash !== "#/settings") location.hash = "#/settings";
  else {
    // explicit navigation: a focused "Settings" field would otherwise keep the view from re-rendering; focus that was
    // on a tab moves to the new one
    const a = document.activeElement;
    if (a?.closest?.("[data-settings-tab]")) { pendingTabFocus = id; a.blur(); }
    else if (a?.closest?.("main .feed")) a.blur();
    render();
  }
});

// checkboxes of the core (what to show in "Brief") and of plugins (hub.addOptions, API 1.12) — plugins and modules can
// add their own settings here; they live on the "Look" tab. The choice is per browser; sync() re-reads the values (a
// checkbox that was never touched follows the "Reactions" module)
function optionsGroup(id, title, hint, options, value, set) {
  const sec = el("section", "settings-sec");
  sec.append(el("h3", "day", title), ...(hint ? [el("p", "hint", hint)] : []));
  const list = el("div", "feed-opts"), more = el("details", "set-more"), deep = el("div", "feed-opts");
  keepOpen(more, "look:" + id);   // the group id, not its title: the title is translated
  more.append(el("summary", null, t("web.settings.advanced")), deep);
  const rows = options.map((o) => {
    const l = tickRow(o, value, set);
    (o.adv ? deep : list).append(l);
    return l;
  });
  sec.append(list);
  if (deep.children.length) sec.append(more);
  sec.sync = () => { for (const l of rows) l.sync(); };
  return sec;
}

// hub settings (hub.json and the watchdog) — from the core allowlist (modules/web/settings.py): simple ones up front,
// the rest under "Advanced". A field the owner is currently in is not overwritten by a snapshot
const fields = new Map();   // key -> { set(value), node }

// hint for a group — by the keys of its rows (the server returns the group title in the page language)
const hintOf = (rows) => (rows.some((r) => r.key.startsWith("executors.")) ? t("web.settings.group_executors_hint") : null);
let pluginBox = null, pluginSig = "", feedBox = null, themesBox = null;

let rowsSig = "";
// rebuild when the settings rows change
const generalRows = () => (S.snap.settings || []).filter((r) => !r.place?.startsWith("module:") && r.place !== "hidden");
const sigOf = () => JSON.stringify(generalRows().map(({ value, ...row }) => row));   // options, labels and limits change without a key change
// a snapshot without place (core older than the page) — main fields by name, the rest under "Advanced"
const MAIN = ["title", "timezone", "executors.parallel", "permissions.auto_allow"];
const where = (r) => r.place || (MAIN.includes(r.key) ? "main" : "more");
function buildGeneral(wrap) {
  const rows = generalRows();
  rowsSig = sigOf();
  fields.clear();
  const main = el("section", "settings-sec"), list = el("div", "feed-opts");
  for (const r of rows.filter((r) => where(r) === "main")) { const f = settingRow(r); fields.set(r.key, f); list.append(f.node); }
  main.append(list);
  // one "Advanced" per tab, with group subheadings inside
  const more = el("details", "set-more");
  keepOpen(more, "general:more");   // rebuilt when the rows change
  more.append(el("summary", null, t("web.settings.advanced")));
  const rest = rows.filter((r) => where(r) !== "main");
  for (const g of [...new Set(rest.map((r) => r.group))]) {
    const box = el("div", "feed-opts");
    const mine = rest.filter((r) => r.group === g), hint = hintOf(mine);
    for (const r of mine.filter((r) => !r.agent)) { const f = settingRow(r); fields.set(r.key, f); box.append(f.node); }
    more.append(el("h4", "set-sub", g), ...(hint ? [el("p", "hint", hint)] : []), box, ...agentsBlock(mine));
  }
  instances ||= instancesSection();   // the hubs of this machine, each with its password: its own state (the forms, a running install)
  wrap.replaceChildren(main, instances, ...(rest.length ? [more] : []),
    el("p", "hint", t("web.settings.where_else")));
}
let instances = null;

// The "Own pair per agent" block of the executors group: a line per agent (the server sends two rows each, model and effort)
function agentsBlock(rows) {
  const model = rows.filter((r) => r.agent && r.field === "model");
  if (!model.length) return [];
  const box = el("div", "feed-opts");
  for (const m of model) {
    const e = rows.find((r) => r.agent === m.agent && r.field === "effort");
    const f = agentRow(m, e);
    fields.set(m.key, { set: f.setModel });
    fields.set(e.key, { set: f.setEffort });
    box.append(f.node);
  }
  return [el("h5", "set-sub", t("web.settings.executors_agents")), el("p", "hint", t("web.settings.executors_agents_hint")), box];
}

function generalSection() {
  const wrap = el("div", "settings-groups");
  buildGeneral(wrap);
  return wrap;
}

function refreshGeneral(wrap) {
  if (sigOf() !== rowsSig && !wrap.contains(document.activeElement)) buildGeneral(wrap);
  for (const r of S.snap.settings || []) fields.get(r.key)?.set(r.value);
  refreshInstances();
}

// "Look": theme and text size (themes.js; a theme's own settings under its row), the "Brief" feed, plugin checkboxes
// (hub.addOptions without themes)
function lookSection() {
  const wrap = el("div", "settings-groups");
  themesBox = themesSection();
  wrap.append(themesBox, optionsGroup("brief", t("web.settings.brief_title"), t("web.settings.brief_hint"),
    [...BRIEF, { key: "shortPaths", adv: true, label: t("web.brief.short_paths"),
      hint: t("web.brief.short_paths_hint") }, { key: "atEnd", adv: true, label: t("web.brief.at_end"), hint: t("web.brief.at_end_hint") }],
    (k) => (k === "shortPaths" ? S.shortPaths : k === "atEnd" ? S.feedAtEnd : k === "resolved" ? foldOn() : S.brief[k]),
    (k, on) => { if (k === "shortPaths") setShortPaths(on); else if (k === "atEnd") setFeedAtEnd(on); else setBrief(k, on); render(); }));
  feedBox = wrap.lastChild;
  pluginBox = el("div", "settings-groups");
  pluginSig = "";
  wrap.append(pluginBox);
  return wrap;
}

function refreshLook() {
  feedBox?.sync();
  themesBox?.sync();
  const groups = pluginOptions(), sig = JSON.stringify(groups.map((g) => [g.ext, g.sig]));
  if (pluginBox && sig !== pluginSig) {   // plugins load after the page — their groups appear later
    pluginSig = sig;
    pluginBox.replaceChildren(...groups.map((g) => optionsGroup(g.ext, g.title, g.hint || t("web.settings.plugin_hint", { ext: g.ext }), g.options,
      (k) => optionValue(g.ext, k), (k, on) => setOption(g.ext, k, on))));
  }
  for (const group of pluginBox?.children || []) group.sync?.();
}

// a plugin declared its ticks after "Look" was drawn: only the tab's groups are redrawn, not the page — a plugin that declares
// them in its own render (with a changing hint, say) would otherwise loop the page
addEventListener("hub-options", () => { if (S.selected === ":settings" && pluginBox?.isConnected) refreshLook(); });

function current(list) {
  if (location.hash === "#/browsers") localStorage.setItem(TAB, "modules");   // old address — browsers are now under "Modules"
  if (["jev", "browsers"].includes(localStorage.getItem(TAB))) localStorage.setItem(TAB, "modules");
  if (localStorage.getItem(TAB) === "updates") localStorage.setItem(TAB, "about");
  return list.find((x) => x.id === localStorage.getItem(TAB)) || list[0];
}

export function settingsView() {
  const focusedTab = document.activeElement?.closest?.("[data-settings-tab]")?.dataset.settingsTab
    || (document.activeElement === document.body ? pendingTabFocus : null);
  pendingTabFocus = focusedTab;
  const wrap = el("div");
  const h = el("div", "head");
  const back = el("button", "btn quiet back", t("web.nav.all_sessions"));
  back.type = "button";
  back.addEventListener("click", toList);
  const list = tabs(), cur = current(list);
  const bar = el("div", "seg tabs");
  bar.setAttribute("role", "tablist");
  bar.setAttribute("aria-label", t("web.settings.sections"));
  for (const tab of list) {
    const b = el("button", null, tab.title);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.dataset.settingsTab = tab.id;
    b.setAttribute("aria-pressed", String(tab.id === cur.id));
    b.setAttribute("aria-selected", String(tab.id === cur.id));
    b.addEventListener("click", () => { localStorage.setItem(TAB, tab.id); if (location.hash !== "#/settings") location.hash = "#/settings"; render(); });
    bar.append(b);
  }
  h.append(back, el("h2", null, t("web.settings.tab.general")), bar);
  const f = el("div", "feed"), inner = el("div", "feed-inner");
  const node = cur.node || (cur.keep ? (keep[cur.id] ||= cur.make()) : cur.make());
  if (cur.keep && cur.refresh) cur.refresh(node);   // the tab keeps its fields (focus, typed text), values come from the snapshot
  // above every tab: the red band about own edits of the core
  inner.append(...[localEditsBand(), node].filter(Boolean));
  f.append(inner);
  wrap.append(h, f);
  // the view is moved into the page after returning: focus goes to the tab that held it before the move; if the owner
  // managed to move to another field within the frame, it stays there
  if (focusedTab) requestAnimationFrame(() => {
    if (pendingTabFocus === focusedTab) pendingTabFocus = null;
    if (bar.isConnected && document.activeElement === document.body)
      [...bar.children].find((b) => b.dataset.settingsTab === focusedTab)?.focus({ preventScroll: true });
  });
  return wrap;
}

// A link at the bottom of the side list; if the browser does not respond, it is visible from here too
