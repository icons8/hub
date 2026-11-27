// Settings → Plugins — a catalog (plugins are shown as a catalog with descriptions; each plugin has an icon, a
// description, what it does and why; a plugin is connected after a review, so that the hub looks at the plugin code
// itself and connects it only afterwards, with the user's consent).
// At the top — search and filters (kind, catalog). Next — what is installed: a compact list, quiet actions, trouble on
// a single line. Below — what can be installed, in the same compact row, grouped by kind (plugins, themes, core
// modules): entries of the connected marketplaces (/api/marketplaces, marketplace spec §3.13) and plugins by link
// under review (/api/plugins/search). At the bottom — the marketplaces
// themselves: the owner connects and disconnects them with a command, the page only displays them. Page code is
// "full access to the page" wherever it exists.
import { el } from "./dom.js";
import { clock } from "./clock.js";
import { S } from "./state.js";
import { post } from "./api.js";
import { problems } from "./ext.js";
import { locale, t } from "./i18n.js";

const KINDS = (p) => p.kinds || [p.web && t("web.plugins.kind_page"), p.run && t("web.plugins.kind_process"), (p.themes || []).length && t("web.plugins.kind_themes")].filter(Boolean);
const LACK = { icon: t("web.plugins.lack_icon"), description: t("web.plugins.lack_description"), why: t("web.plugins.lack_why") };
const VERDICT = {
  checking: [t("web.plugins.verdict_checking"), ""],
  ok: [t("web.plugins.verdict_ok"), "ok"],
  risky: [t("web.plugins.verdict_risky"), "warn"],
  bad: [t("web.plugins.verdict_bad"), "bad"],
};
// a catalog entry's rights in words (as the catalog words them; what the code asks for, the core computes from the manifest at install)
const RIGHT = { page: t("web.plugins.right_page"), process: t("web.plugins.kind_process"), themes: t("web.plugins.kind_themes"),
  skills: t("web.plugins.right_skills") };
const GROUPS = [["plugin", t("web.plugins.group_plugins")], ["theme", t("web.plugins.group_themes")], ["module", t("web.plugins.group_modules")]];
let available = null, loading = false, loadError = "", query = "";
let mp = null, mpLoading = false;   // connected marketplaces: {catalogs, entries, official}; an old server — empty
let kindBy = "all", catBy = "all", categoryBy = "all";  // kind, catalog and category filters — for the lifetime of the page
// catalog order (by popularity, from the statistics server): popularity is how many hubs installed the plugin, from
// the hub statistics summary at release time (only hubs that enabled statistics)
const SORT = "hub.pluginSort";
let sortBy = (() => { try { return localStorage.getItem(SORT) || "popular"; } catch { return "popular"; } })();
const byName = (a, b) => (a.name || "").localeCompare(b.name || "", locale);
const sorted = (list) => [...list].sort(sortBy === "name" ? byName : (a, b) => (b.hubs || 0) - (a.hubs || 0) || byName(a, b));
let updates = null, updLoading = false;  // plugin key -> {commit, version, review?}: the author has a newer one
const notes = new Map();  // plugin -> {text, warn}: the reply to a button survives a redraw of the tab
// the outcome of the last action — at the top of the tab and after a page reload (so it does not look as if you enable
// something and nothing happens): once installed or enabled, the page reloads with the plugin by itself
const NOTICE = "hub.pluginNotice";
let notice = (() => { try { const n = JSON.parse(sessionStorage.getItem(NOTICE) || "null"); return n && Date.now() - n.ts < 120000 ? n : null; } catch { return null; } })();
function setNotice(n) {
  notice = n;
  try { n ? sessionStorage.setItem(NOTICE, JSON.stringify(n)) : sessionStorage.removeItem(NOTICE); } catch { /* unavailable */ }
}
function noticeBox() {
  if (!notice) return el("div", "plugin-notice-slot");
  const box = el("div", "plugin-notice" + (notice.warn ? " warn" : ""));
  box.setAttribute("role", "status");
  box.append(el("span", null, notice.warn ? notice.text : `✓ ${notice.text}`));
  const x = el("button", "btn quiet small", t("web.plugins.hide"));
  x.type = "button";
  x.addEventListener("click", () => { setNotice(null); box.replaceWith(noticeBox()); });
  box.append(x);
  return box;
}

function noteFor(key) {
  const n = el("p", "plugin-note"), got = notes.get(key);
  if (got) { n.textContent = got.text; n.classList.toggle("warn", got.warn); }
  n.dataset.key = key;
  if (asks.has(key)) { n.textContent = ""; n.append(askBox(key, n)); }
  return n;
}

// an icon is the same node between re-renders: the tab is rebuilt on every core snapshot (about once a second while
// sessions work), and Safari showed a new <img> blank for a frame (the icons flickered). A node can sit in only one
// place: the same icon twice in one re-render gets a second node. A new version is a new node: the address stays the same
const icons = new Map(), iconUses = new Map();   // key → nodes; key → how many were taken in this re-render (pluginsSection clears it)
function icon(p) {
  const src = p.icon_url || p.icon;
  if (!src) return letterIcon(el("div", "plugin-icon"), p);
  const key = `${src} ${p.commit || p.version || ""}`, n = iconUses.get(key) || 0, list = icons.get(key) || [];
  iconUses.set(key, n + 1);
  if (!list[n]) {
    const box = el("div", "plugin-icon"), img = el("img");
    img.alt = "";
    img.loading = "lazy";
    img.src = src;
    img.addEventListener("error", () => { img.remove(); letterIcon(box, p); }, { once: true });
    box.append(img);
    list[n] = box;
  }
  icons.delete(key);
  icons.set(key, list);
  if (icons.size > 300) icons.delete(icons.keys().next().value);
  return list[n];
}
function letterIcon(box, p) {
  box.textContent = (p.name || "?").replace(/^.*:/, "")[0].toUpperCase();
  box.classList.add("letter");
  return box;
}

// the answer to a button can arrive after the tab was redrawn (the row the click started from is gone): the outcome is
// already in notes/asks, so the tab is redrawn from them (only while it is on the screen) instead of writing into a
// detached node; a button pressed again while its request is in flight is ignored
const redrawIfGone = (note) => { if (!note.isConnected && wrap?.isConnected) window.dispatchEvent(new Event("hub-plugins")); };
const inFlight = new Set();
// a button that sends a task to a session stays held after the answer too (and after the tab is redrawn, when the button
// is built anew): quick repeated clicks do not send the same task again. An error frees it at once
const SENDS = new Set(["/api/plugins/fix", "/api/plugins/review"]), HOLD = 8000;
const sentAt = new Map();   // path + body -> when it was pressed
const held = (key) => Date.now() - (sentAt.get(key) || 0) < HOLD;

function action(label, path, body, note, cls = "btn quiet small", after) {
  const b = el("button", cls, label);
  b.type = "button";
  const sends = SENDS.has(path), key = path + JSON.stringify(body);
  // built anew during the hold (the tab redrew): free it once the hold is over and the press has its answer (an error
  // frees it at once), as the pressed button is freed
  const busyOf = `${note.dataset.key} ${path}`;
  if (sends && (held(key) || inFlight.has(busyOf))) {
    b.disabled = true;
    const free = () => { if (held(key) || inFlight.has(busyOf)) setTimeout(free, 250); else b.disabled = false; };
    setTimeout(free, 250);
  }
  b.addEventListener("click", async () => {
    const busy = `${note.dataset.key} ${path}`;
    if (inFlight.has(busy) || (sends && held(key))) return;
    inFlight.add(busy);
    if (sends) sentAt.set(key, Date.now());
    b.disabled = true;
    const r = await post(path, body);
    inFlight.delete(busy);
    if (sends && r.error) sentAt.delete(key);
    const got = { text: r.data?.wider ? r.error : r.error ? t("web.plugins.failed", { error: r.error }) : (r.data?.text || r.text || t("web.plugins.done")), warn: !!r.error };
    notes.set(note.dataset.key, got);
    note.textContent = got.text;
    note.classList.toggle("warn", got.warn);
    asks.delete(note.dataset.key);
    if (r.data?.wider) {  // the new version asks for more than allowed: show what exactly, plus "Allow and update"
      asks.set(note.dataset.key, { list: r.data.wider, path, body: { ...body, accept: true }, after });
      note.textContent = "";
      note.append(askBox(note.dataset.key, note));
    }
    setNotice({ ...got, ts: Date.now() });
    wrap?.querySelector(".plugin-notice, .plugin-notice-slot")?.replaceWith(noticeBox());
    if (sends && !r.error) setTimeout(() => { b.disabled = false; }, Math.max(0, HOLD - (Date.now() - sentAt.get(key))));
    else b.disabled = false;
    if (!r.error && after) after();
    else if (r.error && body.at) load(true);   // the catalog was updated — fetch rights and commit again
    else redrawIfGone(note);   // after() and load() redraw the tab themselves
  });
  return b;
}

// an update with new rights (a process or another command, page code, executable skill files) waits for an explicit
// "yes", and likewise what will be installed when the catalog did not name the rights (preview). The question lives in
// asks: a redraw of the tab does not lose it
const asks = new Map();   // row key -> {list, path, body, after, preview?}
function askBox(key, note) {
  const a = asks.get(key);
  const box = el("div", "plugin-review" + (a.preview ? "" : " warn"));
  box.append(el("b", null, t(a.preview ? "web.plugins.preview_head" : "web.plugins.consent_head")));
  const list = el("ul");
  for (const w of a.list) list.append(el("li", null, w));
  box.append(list);
  if (!a.preview) box.append(el("p", null, t("web.plugins.consent_keep")));
  if (!(a.preview && a.list.some((w) => w.startsWith("!")))) {   // "! …" in the preview — the core will not install such an entry
    box.append(action(t(a.preview ? "web.plugins.install_with_rights" : "web.plugins.consent_allow"), a.path, a.body, note,
      a.preview ? "btn small primary" : "btn small danger", a.after));
  }
  return box;
}

// page code has full access to the page (there is no isolation): we always say so, whether or not the plugin declared
// page_access; on a single line (a long list stays compact), what exactly that means goes in the tooltip and in the
// list of rights before installation
function fullAccess() {
  const p = el("p", "plugin-warn", `⚠ ${t("web.plugins.full_access_short")}`);
  p.title = t("web.plugins.full_access");
  return p;
}

const reload = () => load(true);
const author = (p) => (p.source || p.repo || "").split("/")[0];
// a link to the plugin's repository on GitHub (so the repos can be followed)
function repoLink(repo, text) {
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo || "")) return null;
  const a = el("a", "plugin-repo", text || `github.com/${repo}`);
  a.href = `https://github.com/${repo}`;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.title = t("web.plugins.repo_open");
  return a;
}

// the kind of an entry — for the filter and the group: a core module, a theme (a plugin with themes), anything else is a plugin
const isTheme = (p) => !!((p.themes || []).length || p.rights?.themes || p.category === "themes" ||
  (p.kinds || []).some((k) => [t("web.plugins.kind_themes"), "themes"].some((w) => k.toLowerCase() === w.toLowerCase())));   // a kind from the previous list — spelled by the core in the owner's language
const kindOf = (p) => (p.type === "module" ? "module" : isTheme(p) ? "theme" : "plugin");
// category: the catalog entry's category field, the list is shared with the validator (core/marketplace.CATEGORIES);
// without one, or an unknown one — a theme goes to "Look and themes", anything else to "Other"
const CATEGORIES = [["look", t("web.plugins.cat_look")], ["feed", t("web.plugins.cat_feed")], ["sessions", t("web.plugins.cat_sessions")],
  ["notify", t("web.plugins.cat_notify")], ["work", t("web.plugins.cat_work")], ["tools", t("web.plugins.cat_tools")],
  ["fun", t("web.plugins.cat_fun")], ["other", t("web.plugins.cat_other")]];
const CATEGORY = new Map(CATEGORIES);
const categoryOf = (p) => (CATEGORY.has(p.category) ? p.category : isTheme(p) ? "look" : "other");
const official = () => mp?.official || "agents-hub";
const entryOf = (key) => (mp?.entries || []).find((e) => e.installed === key);
const sameRepo = (a, b) => !!a && !!b && a.toLowerCase() === b.toLowerCase();

function tag(r, p, cat) {
  r.dataset.kind = kindOf(p);
  r.dataset.cat = cat || "";
  r.dataset.category = categoryOf(p);
  r.dataset.text += " " + CATEGORY.get(r.dataset.category).toLowerCase();
}

function installedRow(p) {
  const r = el("article", "plugin-row" + (p.disabled ? " off" : ""));
  // from the catalog: installed from it (market) or installed before catalogs from a source the catalog pinned (the
  // core matches them by source) — updated to the catalog's version
  const entry = entryOf(p.name) || null;
  const from = p.market || entry?.catalog;
  r.dataset.text = [p.name, p.source, p.description, p.why, from, KINDS(p).join(" ")].join(" ").toLowerCase();
  const old = (available || []).find((a) => sameRepo(a.repo, p.source));
  tag(r, { ...p, category: entry?.category ?? old?.category }, from || old?.cat);
  const main = el("div", "plugin-main");
  const title = el("div", "plugin-title");
  title.append(el("h4", null, p.name.replace(/^.*:/, "")));
  if (author(p)) title.append(el("span", "plugin-author", author(p)));
  if (p.bundled) title.append(el("span", "plugin-author", t("web.plugins.bundled")));
  title.append(el("span", "chip " + (p.disabled ? "" : "ok"), p.disabled ? t("web.plugins.off") : t("web.plugins.on")));
  // newer: for one installed from a catalog — the catalog's version, for older ones — the author's head (/api/plugins/updates)
  const up = entry ? (entry.newer ? { version: entry.version } : null) : (updates || {})[p.name];
  if (up) title.append(el("span", "chip warn", up.version ? t("web.plugins.update_to", { version: up.version }) : t("web.plugins.update_newer")));
  main.append(title, el("p", "plugin-what", p.description || t("web.plugins.no_description")));
  const meta = [p.version && `v${p.version}`, from && t("web.plugins.from_catalog", { name: from }),
    p.commit && t("web.plugins.commit", { commit: p.commit })].filter(Boolean).join(" · ");
  const link = repoLink(p.source);
  if (meta || link) {
    const m = el("p", "plugin-meta", meta);
    if (link) m.append(meta ? " · " : "", link);
    main.append(m);
  }
  const warns = [p.warning, p.missing === true && t("web.plugins.missing_dir"), entry?.revoked && t("web.plugins.revoked", { why: entry.revoked }),
    // its process keeps exiting: hub-up restarts it, the plugin only looks installed and fine
    p.exits && t("web.plugins.process_exits", { code: p.exits.code, count: p.exits.count }),
    ...(problems.get(p.name) || [])].filter(Boolean);
  const note = noteFor(p.name);
  if (!p.bundled && p.web) main.append(fullAccess());
  if (warns.length) {
    const w = el("p", "plugin-warn", `⚠ ${warns.length > 1 ? t("web.plugins.warn_more", { first: warns[0], count: warns.length - 1 }) : warns[0]}`);
    w.title = warns.join("\n");
    main.append(w);
  }
  // what the new version asks for beyond what was approved — in the catalog's words; the core gives the exact list on "Update"
  if (entry?.newer && entry.wider?.length) main.append(el("p", "plugin-warn", `⚠ ${t("web.plugins.asks_more", { list: rightsText(entry.wider) })}`));
  const bar = el("div", "plugin-actions");
  if (warns.length && p.source && !entry) bar.append(action(t("web.plugins.fix_pr"), "/api/plugins/fix", { name: p.name }, note, "btn small"));
  bar.append(action(p.disabled ? t("web.plugins.enable") : t("web.plugins.disable"), "/api/plugins/toggle", { name: p.name, enabled: p.disabled }, note));
  if (entry?.newer && !entry.revoked) {  // from a catalog — exactly its version: "Update", new rights — "Allow and update"
    const at = (mp.catalogs || []).find((c) => c.name === from)?.commit;   // the version and rights belong to the commit shown on screen
    bar.append(action(t("web.plugins.update"), "/api/plugins/update", { name: p.name, ...(at ? { at } : {}) }, note, "btn small primary", reload));
  } else if (up && !from) {  // update only when there is something to update; check a new version first
    const rv = up.review;
    if (rv) {
      const [label, cls] = VERDICT[rv.state] || [rv.state, ""];
      const box = el("div", "plugin-review " + cls);
      box.append(el("b", null, t("web.plugins.new_version", { verdict: label.toLowerCase(), commit: (rv.commit || "").slice(0, 7) })));
      if (rv.summary) box.append(el("p", null, rv.summary));
      main.append(box);
    }
    if (rv && ["ok", "risky"].includes(rv.state))
      bar.append(action(t("web.plugins.update_reviewed"), "/api/plugins/update", { name: p.name, reviewed: true }, note, "btn small primary", refreshUpdates));
    else if (!rv || rv.state === "bad")
      bar.append(action(rv ? t("web.plugins.check_again") : t("web.plugins.check_update"), "/api/plugins/review", { repo: p.source }, note, "btn small primary", refreshUpdates));
    // without a review this core installs nothing outside catalogs (unchecked: false — it would refuse); a server of an
    // earlier release still running on the new files (until its restart) may send true or no field — and installs it
    if (!(rv && rv.state === "bad") && up.unchecked !== false)
      bar.append(action(t("web.plugins.update_unchecked"), "/api/plugins/update", { name: p.name }, note, "btn quiet small", refreshUpdates));
  }
  if (p.source) bar.append(action(t("web.plugins.remove"), "/api/plugins/remove", { name: p.name }, note, "btn quiet small danger", reload));
  main.append(note);
  r.append(icon(p), main, bar);
  return r;
}

const rightsText = (pairs) => pairs.map(([k, what]) => (RIGHT[k] || k) + (what ? `: ${what}` : "")).join(", ");
// an entry's rights from the catalog's words -> words; a module has its own kind; if the catalog did not name them, we say so
function entryRights(e) {
  if (e.type === "module") return [t("web.plugins.kind_module")];
  if (!e.rights) return [t("web.plugins.rights_unknown")];
  return Object.entries(e.rights).filter(([, v]) => v).map(([k, v]) => (RIGHT[k] || k) + (typeof v === "string" ? `: ${v}` : ""));
}

// a catalog row: icon, name, author and catalog, what it does, rights, popularity; the action is on the right
function catalogRow(p) {
  const r = el("article", "plugin-row plugin-entry");
  r.dataset.text = [p.name, p.repo, p.cat, p.author, p.description, p.why, ...(p.tags || []), p.type === "module" ? t("web.plugins.kind_module") : "",
    (p.mp ? entryRights(p) : KINDS(p)).join(" ")].join(" ").toLowerCase();
  tag(r, p, p.cat);
  const main = el("div", "plugin-main");
  const title = el("div", "plugin-title");
  title.append(el("h4", null, p.name));
  const by = p.author || author(p);
  if (by) title.append(el("span", "plugin-author", by));
  // official: the catalog's own flag (a team's build has two official catalogs); an older server — by the name
  const ofc = (mp?.catalogs || []).find((c) => c.name === p.cat);
  if (p.cat) title.append(el("span", "chip" + ((ofc ? ofc.official : p.cat === official()) ? " ok" : ""), p.cat));
  else if (p.link) title.append(el("span", "chip", t("web.plugins.by_link")));
  if (p.installed && p.mp) title.append(el("span", "chip ok", t("web.plugins.on")));
  main.append(title);
  const what = el("p", "plugin-what clamp-2", p.description || t("web.plugins.no_description"));
  what.title = p.description || "";
  main.append(what);
  // the "why" is visible too, on a single line
  if (p.why) { const w = el("p", "plugin-why clamp-1", t("web.plugins.why", { why: p.why })); w.title = p.why; main.append(w); }
  const page = (w) => w === RIGHT.page || w.startsWith(t("web.plugins.kind_page"));   // the page is marked below
  const meta = el("p", "plugin-meta", [...(p.mp ? entryRights(p) : KINDS(p)).filter((w) => !page(w)), p.version && `v${p.version}`]
    .filter(Boolean).join(" · "));
  if (p.hubs) {
    const pop = el("span", "plugin-pop", t("web.plugins.hubs", { count: p.hubs }));
    pop.title = t("web.plugins.hubs_title");
    meta.append(" · ", pop);
  }
  const link = repoLink(p.repo, "GitHub");   // short in a catalog row: the author is already in the heading
  if (link) meta.append(" · ", link);
  if (p.page_access === "full" || p.rights?.page) {   // page code — on the same line, in the warning color
    const full = el("span", "plugin-full", `⚠ ${t("web.plugins.full_access_short")}`);
    full.title = t("web.plugins.full_access");
    meta.append(" · ", full);
  }
  main.append(meta);
  const lack = (p.missing || []).map((k) => LACK[k]).filter(Boolean);
  if (lack.length) main.append(el("p", "plugin-lack", t("web.plugins.lack", { list: lack.join(", ") })));
  if (p.warning) main.append(el("p", "plugin-warn", `⚠ ${p.warning}`));
  const bar = el("div", "plugin-actions");
  const note = noteFor(p.mp ? `${p.name}@${p.cat}` : p.repo);
  if (p.mp) entryActions(p, main, bar, note);
  else oldActions(p, main, bar, note);
  main.append(note);
  r.append(icon(p), main, bar);
  return r;
}

// an entry of a connected catalog: installed at exactly its commit (core/mpinstall)
function entryActions(p, main, bar, note) {
  const label = `${p.name}@${p.cat}`;
  if (p.revoked) { main.append(el("p", "plugin-warn", `⚠ ${t("web.plugins.revoked", { why: p.revoked })}`)); return; }
  // a skill set entry of a Claude Code or Codex catalog the hub pins itself at install (core/mpinstall._skillset)
  if (!p.pinned && p.type !== "skillset") { main.append(el("p", "plugin-warn", `⚠ ${t("web.plugins.unpinned")}`)); return; }
  if (p.type === "module") {   // a module starts right away — a command installs it and prints its rights after installation
    if (!p.installed || p.newer) main.append(el("p", "plugin-meta", t("web.plugins.module_command", { command: `hub marketplace install ${label}` })));
    return;
  }
  if (p.installed) return;   // already installed — its actions are in the "Installed" list
  if (!(mp.catalogs || []).find((c) => c.name === p.cat)?.official) main.append(el("p", "plugin-warn", `⚠ ${t("web.plugins.third_party")}`));
  // the rights the user sees belong to the catalog commit at: if the catalog was updated before the click, the core refuses rather than installing something else
  const at = (mp.catalogs || []).find((c) => c.name === p.cat)?.commit;
  bar.append(previewButton(label, note));
}

// "Install" first shows what the entry will get (the manifest at the catalog commit: all rights, including those the
// catalog did not name or named more briefly), then installs with them
function previewButton(label, note) {
  const b = el("button", "btn small primary", t("web.plugins.install"));
  b.type = "button";
  b.addEventListener("click", async () => {
    const busy = `${note.dataset.key} preview`;
    if (inFlight.has(busy)) return;
    inFlight.add(busy);
    b.disabled = true;
    const r = await post("/api/plugins/install", { entry: label, preview: true });
    inFlight.delete(busy);
    b.disabled = false;
    note.textContent = "";
    note.classList.toggle("warn", !!r.error);
    asks.delete(note.dataset.key);
    if (r.error) {
      const got = { text: t("web.plugins.failed", { error: r.error }), warn: true };
      notes.set(note.dataset.key, got);
      note.textContent = got.text;
    } else {
      notes.delete(note.dataset.key);
      asks.set(note.dataset.key, { preview: true, list: r.data.preview || [], path: "/api/plugins/install",
        body: { entry: label, accept: true, ...(r.data.at ? { at: r.data.at } : {}) }, after: reload });
      note.append(askBox(note.dataset.key, note));
    }
    redrawIfGone(note);
  });
  return b;
}

// plugins by link: "Review and install", then "Install the reviewed one"
function oldActions(p, main, bar, note) {
  const rv = p.review;
  if (rv) {
    const [label, cls] = VERDICT[rv.state] || [rv.state, ""];
    const box = el("div", "plugin-review " + cls);
    box.append(el("b", null, `${label}${rv.commit ? ` · ${rv.commit.slice(0, 7)}` : ""}`));
    if (rv.summary) box.append(el("p", null, rv.summary));
    main.append(box);
  }
  if (p.compat === "incompatible") {
    note.textContent = t("web.plugins.incompatible");
  } else if (rv && ["ok", "risky"].includes(rv.state)) {
    bar.append(action(t("web.plugins.install_reviewed"), "/api/plugins/install", { repo: p.repo, reviewed: true }, note, "btn small primary", reload));
  } else if (!rv || rv.state === "bad") {
    bar.append(action(rv ? t("web.plugins.check_again") : t("web.plugins.check_install"), "/api/plugins/review", { repo: p.repo }, note, "btn small primary", reload));
  }
}

function loadUpdates(fresh) {
  if (updLoading) return;
  updLoading = true;
  fetch(`/api/plugins/updates${fresh ? "?fresh=1" : ""}`).then((r) => r.json()).then((j) => { updates = j.updates || {}; })
    .catch(() => { updates = updates || {}; }).finally(() => {
      updLoading = false;
      window.dispatchEvent(new Event("hub-plugins"));
    });
}
const refreshUpdates = () => loadUpdates(false);

// The catalog is fetched ahead (once the page is idle) and kept in memory: opening the tab shows the list at once. A
// repeated fetch never clears what is already shown: the old list stays until the new answer replaces it, and a failed
// fetch leaves it as it is. "Loading…" appears only while there is nothing at all yet.
const FRESH_MS = 30 * 60 * 1000;   // the list is refetched in the background this often (and when the tab opens, if older)
let triedAt = 0;
function load(force) {
  const stale = Date.now() - triedAt > FRESH_MS;
  if (!loading && (!available || force || stale)) {
    loading = true;
    triedAt = Date.now();
    fetch("/api/plugins/search?q=").then((r) => r.json()).then((j) => {
      const list = j.plugins || [];
      if (j.error && !list.length && available?.length) return;   // the answer is an error: the shown list stays
      available = list;
      loadError = j.error || "";
    }).catch(() => {
      if (available) return;
      available = [];
      loadError = t("web.plugins.no_connection");
    }).finally(() => {
      loading = false;
      window.dispatchEvent(new Event("hub-plugins"));
    });
  }
  if (!mpLoading && (!mp || force || stale)) {   // a server that predates marketplaces answers 404 — no block, the rest as before
    mpLoading = true;
    fetch("/api/marketplaces").then((r) => (r.ok ? r.json() : { catalogs: [], entries: [], none: true })).then((j) => { mp = j; })
      .catch(() => { mp ||= { catalogs: [], entries: [], none: true }; }).finally(() => {
        mpLoading = false;
        window.dispatchEvent(new Event("hub-plugins"));
      });
  }
}
// in the background, when the page has settled (the catalog can take a while: the tab should not wait for it)
if (window.requestIdleCallback) window.requestIdleCallback(() => load(), { timeout: 5000 });
else setTimeout(() => load(), 2000);
setInterval(() => load(true), FRESH_MS);

// while something is under review, keep checking for the verdict
setInterval(() => {
  if (S.selected !== ":settings") return;
  if ((available || []).some((p) => p.review?.state === "checking")) load(true);
  if (Object.values(updates || {}).some((u) => u.review?.state === "checking")) loadUpdates(false);
}, 15000);

// a GitHub repo link in the field (just paste a github link to a plugin — installed only through a review)
const repoOf = (text) => {
  const m = /^\s*(?:https?:\/\/)?(?:www\.)?github\.com\/([A-Za-z0-9-]+)\/([A-Za-z0-9._-]+?)(?:\.git)?(?:[/#?].*)?\s*$/.exec(text);
  return m && m[2].replace(/\./g, "") ? `${m[1]}/${m[2]}` : null;
};

function linkBox(full) {
  const box = el("div", "plugin-link");
  const note = noteFor(`link:${full}`);
  box.append(el("p", null, t("web.plugins.link_note", { repo: full })));
  const bar = el("div", "plugin-actions"), sent = notes.get(`link:${full}`);
  // sent for review — no button any more: a card with the verdict will appear in the catalog below
  if (!sent || sent.warn) bar.append(action(t("web.plugins.check_install"), "/api/plugins/review", { repo: full }, note, "btn small primary", reload));
  box.append(bar, note);
  return box;
}

function filter(root) {
  const link = repoOf(query), slot = root.querySelector(".plugin-link-slot");
  if (slot) slot.replaceChildren(...(link ? [linkBox(link)] : []));
  const words = link ? [] : query.toLowerCase().split(/\s+/).filter(Boolean);
  let shown = 0;
  for (const c of root.querySelectorAll(".plugin-row:not(.plugin-mp):not(.plugin-engine)")) {
    const ok = words.every((w) => c.dataset.text.includes(w)) && (kindBy === "all" || c.dataset.kind === kindBy) &&
      (catBy === "all" || c.dataset.cat === catBy) && (categoryBy === "all" || c.dataset.category === categoryBy);
    c.hidden = !ok;
    shown += ok;
  }
  // Claude and Codex plugins: by the words of the search; they have no category or catalog of the shared list
  for (const c of root.querySelectorAll(".plugin-engine")) {
    const ok = words.every((w) => c.dataset.text.includes(w)) && ["all", "plugin"].includes(kindBy) && catBy === "all" && categoryBy === "all";
    c.hidden = !ok;
    shown += ok;
  }
  // a group with no visible rows has no heading; the Claude and Codex section — only while the search hides all its rows
  for (const g of root.querySelectorAll(".plugin-group")) g.hidden = !g.querySelector(".plugin-row:not([hidden])");
  for (const g of root.querySelectorAll(".plugin-engines")) g.hidden = !!g.querySelector(".plugin-row") && !g.querySelector(".plugin-row:not([hidden])");
  const none = root.querySelector(".plugin-none");
  if (none) none.hidden = shown > 0 || !(words.length || kindBy !== "all" || catBy !== "all" || categoryBy !== "all") || !!link;
}

let wrap = null, input = null, body = null, picks = null;
// search the catalog by words (from Themes — "Find themes in the plugin catalog")
export function findPlugins(q) {
  query = q;
  if (input) input.value = q;
}

function select(cls, label, options, value, set) {
  const pick = el("select", "plugin-sort " + cls);
  pick.setAttribute("aria-label", label);
  for (const [v, text] of options) {
    const o = el("option", null, text);
    o.value = v;
    o.selected = v === value;
    pick.append(o);
  }
  pick.addEventListener("change", () => { set(pick.value); filter(body); });
  return pick;
}

// marketplaces: what is connected and from where; connecting and disconnecting is done by a command (or by telling the hub), the page only displays them
function marketsBlock() {
  const parts = [el("h3", "plugin-section", t("web.plugins.markets_head"))];
  if (mp.error) parts.push(el("p", "plugin-warn bad", `⚠ ${t("web.plugins.load_failed", { error: mp.error })}`));
  const rows = [...(mp.catalogs || [])];
  if (rows.length) {
    const list = el("div", "plugin-list");
    for (const c of rows) {
      const r = el("article", "plugin-row plugin-mp");
      const main = el("div", "plugin-main"), title = el("div", "plugin-title");
      title.append(el("h4", null, c.name), el("span", "chip" + (c.official ? " ok" : ""), c.official ? t("web.plugins.official") : t("web.plugins.third_party_short")));
      main.append(title);
      if (c.description) main.append(el("p", "plugin-what clamp-2", c.description));
      const when = c.updated ? clock(c.updated, locale, { day: "numeric", month: "short" }) : "";
      main.append(el("p", "plugin-meta", [c.source, t("web.plugins.entries", { count: c.entries || 0 }),
        c.skipped && t("web.plugins.skipped", { count: c.skipped }), when && t("web.plugins.updated", { when })].filter(Boolean).join(" · ")));
      for (const bad of [c.problem, c.error && t("web.plugins.not_updated", { error: c.error })].filter(Boolean)) main.append(el("p", "plugin-warn", `⚠ ${bad}`));
      const mark = el("div", "plugin-icon letter", c.name[0].toUpperCase());
      r.append(mark, main, el("div", "plugin-actions"));
      list.append(r);
    }
    parts.push(list);
  }
  const how = el("p", "hint", t("web.plugins.markets_how"));
  parts.push(how);
  return parts;
}

// "Claude and Codex plugins": what the hub installed from the engines' own catalogs (the built-in claude-plugins-official
// and openai-curated). They are not in the shared list: the hub installs them when asked and gives each to the sessions
// that need it (hub tools grant). Per plugin: its catalog, what is inside (skills, commands, MCP servers), which sessions
// have it, what will not work as shipped (a placeholder instead of a server's sign-in data), "Give to a session…" and
// "Remove". The list comes with /api/marketplaces (engine); a server of an earlier release (until its restart) has no such
// field — the rows come from the snapshot's catalog then, without what is inside
const ENGINE = new Set(["claude-plugins-official", "openai-curated"]);
const engineCat = (name) => !!name && ((mp?.catalogs || []).find((c) => c.name === name)?.builtin ?? ENGINE.has(name));
// a session the core grants to: up, or asleep and coming up with the same set (core/tools LIVE + ASLEEP, not retired)
const GRANTABLE = new Set(["starting", "idle", "busy", "dialog", "held", "asleep", "parked", "absent"]);
const giving = new Map();   // plugin key -> the session picked in "Give to a session…" (open while the key is here)
const giveBoxes = new Map();   // plugin key -> its picker node: a redraw of the tab keeps the same select and buttons
const opened = new Set();   // "<key> <kind>" of a parts line opened to its whole list (a tap: a phone has no tooltip)
let focusPick = null;   // the key whose picker was just opened: the focus goes to its session list, not to the page
function enginePlugins() {
  if (Array.isArray(mp?.engine)) return mp.engine;
  return Object.entries(S.snap.catalog || {}).filter(([k, v]) => v.kind === "plugin" && engineCat(k.split("@").pop()))
    .map(([k, v]) => ({ key: k, entry: k.replace(/@[^@]*$/, ""), catalog: k.split("@").pop(), description: v.description || "" }))
    .sort((a, b) => a.key.localeCompare(b.key));
}
const holders = (key) => (S.snap.sessions || []).filter((s) => s.state !== "removed" && (s.tools || []).includes(key));

// one line, cut; a tap or Enter opens the whole list (and closes it again) — it stays open across redraws of the tab
function partLine(id, label, names, count) {
  if (!count) return null;
  const line = el("p", "plugin-meta plugin-part" + (opened.has(id) ? "" : " clamp-1"), `${label}: ${names.join(", ")}${count > names.length ? "…" : ""}`);
  line.title = names.join(", ");
  line.tabIndex = 0;
  line.setAttribute("role", "button");
  line.setAttribute("aria-expanded", String(opened.has(id)));
  const flip = () => {
    if (opened.has(id)) opened.delete(id); else opened.add(id);
    line.classList.toggle("clamp-1", !opened.has(id));
    line.setAttribute("aria-expanded", String(opened.has(id)));
  };
  line.addEventListener("click", flip);
  line.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
  return line;
}

function engineRow(p) {
  const r = el("article", "plugin-row plugin-engine");
  r.dataset.key = p.key;
  r.dataset.text = [p.entry, p.catalog, p.description, ...(p.skills || []), ...(p.commands || []), ...(p.mcp || [])]
    .join(" ").toLowerCase();
  const entry = (mp?.entries || []).find((e) => e.installed === p.key);
  const main = el("div", "plugin-main"), title = el("div", "plugin-title");
  title.append(el("h4", null, p.entry), el("span", "plugin-author", t("web.plugins.from_catalog", { name: p.catalog })));
  const about = p.description || entry?.description || "";
  const what = el("p", "plugin-what clamp-2", about || t("web.plugins.no_description"));
  what.title = about;
  main.append(title, what);
  const c = p.counts || {};
  for (const line of [partLine(`${p.key} skills`, t("web.plugins.engine_skills", { count: c.skills || 0 }), p.skills || [], c.skills),
    partLine(`${p.key} commands`, t("web.plugins.engine_commands", { count: c.commands || 0 }), p.commands || [], c.commands),
    partLine(`${p.key} mcp`, t("web.plugins.engine_mcp", { count: c.mcp || 0 }), p.mcp || [], c.mcp)]) if (line) main.append(line);
  const have = holders(p.key).map((s) => s.name);
  main.append(el("p", "plugin-meta", have.length ? t("web.plugins.engine_granted", { names: have.join(", ") }) : t("web.plugins.engine_granted_none")));
  const warns = [p.missing && t("web.plugins.missing_dir"), entry?.revoked && t("web.plugins.revoked", { why: entry.revoked }),
    ...(p.warnings || []).filter((w) => w.code === "placeholder").map((w) => t("web.plugins.engine_placeholder", { server: w.server, value: w.value }))]
    .filter(Boolean);
  for (const w of warns) main.append(el("p", "plugin-warn", `⚠ ${w}`));
  const note = noteFor(`engine:${p.key}`);
  const bar = el("div", "plugin-actions");
  // a page server of the previous release (until its restart) has no grant path, it would answer 404 — no button
  if (!giving.has(p.key) && mp?.grant) {
    const give = el("button", "btn small", t("web.plugins.engine_give"));
    give.type = "button";
    give.addEventListener("click", () => { giving.set(p.key, ""); focusPick = p.key; window.dispatchEvent(new Event("hub-plugins")); });
    bar.append(give);
  }
  bar.append(action(t("web.plugins.remove"), "/api/plugins/remove", { name: p.key }, note, "btn quiet small danger", reload));
  if (giving.has(p.key)) main.append(giveBox(p.key, have, note));
  main.append(note);
  r.append(entry?.icon ? icon({ ...entry, name: p.entry }) : letterIcon(el("div", "plugin-icon"), { name: p.entry }), main, bar);
  return r;
}

// "Give to a session…": a session that does not have it yet, then "Give" — the owner's word from the page, the same fact
// as hub tools grant <session> <id> (the core brings a free session up with the new set, a sleeping one gets it on wake-up)
function giveBox(key, have, note) {
  const box = giveBoxes.get(key) || el("div", "plugin-give");
  const free = (S.snap.sessions || []).filter((s) => GRANTABLE.has(s.state) && !have.includes(s.name)).map((s) => s.name);
  const sig = free.join("\n");
  if (box.dataset.sig === sig) return box;   // the same choice: the node stays as it is (focus, the picked session)
  box.dataset.sig = sig;
  giveBoxes.set(key, box);
  const cancel = el("button", "btn quiet small", t("web.plugins.engine_give_cancel"));
  cancel.type = "button";
  // closing: the focus leaves the picker first — Settings are not redrawn while a field in them has it
  const close = () => {
    if (box.contains(document.activeElement)) document.activeElement.blur();
    giving.delete(key);
    giveBoxes.delete(key);
  };
  cancel.addEventListener("click", () => { close(); window.dispatchEvent(new Event("hub-plugins")); });
  if (!free.length) { box.replaceChildren(el("span", "hint", t("web.plugins.engine_give_none")), cancel); return box; }
  const pick = el("select", "plugin-sort plugin-give-pick");
  pick.setAttribute("aria-label", t("web.plugins.engine_give_label"));
  for (const name of free) {
    const o = el("option", null, name);
    o.value = name;
    pick.append(o);
  }
  pick.value = free.includes(giving.get(key)) ? giving.get(key) : free[0];
  giving.set(key, pick.value);
  pick.addEventListener("change", () => giving.set(key, pick.value));
  if (focusPick === key) { focusPick = null; requestAnimationFrame(() => pick.focus()); }
  const ok = el("button", "btn small primary", t("web.plugins.engine_give_ok"));
  ok.type = "button";
  ok.addEventListener("click", async () => {
    const busy = `${note.dataset.key} grant`;
    if (inFlight.has(busy)) return;
    inFlight.add(busy);
    ok.disabled = true;
    const r = await post("/api/tools/grant", { session: pick.value, tool: key });
    inFlight.delete(busy);
    ok.disabled = false;
    const got = { text: r.error ? t("web.plugins.failed", { error: r.error }) : (r.data?.text || t("web.plugins.done")), warn: !!r.error };
    notes.set(note.dataset.key, got);
    note.textContent = got.text;
    note.classList.toggle("warn", got.warn);
    if (!r.error) close();
    window.dispatchEvent(new Event("hub-plugins"));
  });
  box.replaceChildren(pick, ok, cancel);
  return box;
}

function engineBlock() {
  const list = enginePlugins();
  const sec = el("section", "plugin-engines");   // the search hides it whole when none of its rows matches
  sec.append(el("h3", "plugin-section", t("web.plugins.engine_head", { count: list.length })));
  if (!list.length) sec.append(el("p", "hint", t("web.plugins.engine_empty")));
  else { const box = el("div", "plugin-list"); box.append(...list.map(engineRow)); sec.append(box); }
  for (const k of [...giving.keys()]) if (!list.some((p) => p.key === k)) { giving.delete(k); giveBoxes.delete(k); }
  return [sec];
}

export function pluginsSection() {
  if (!wrap) {
    wrap = el("div", "plugins");
    input = el("input", "plugin-filter");
    input.type = "search";
    input.placeholder = t("web.plugins.filter_placeholder");
    input.setAttribute("aria-label", t("web.plugins.filter_label"));
    input.value = query;
    input.addEventListener("input", () => { query = input.value; filter(body); });
    picks = el("div", "plugin-filters");
    body = el("div");
    wrap.append(input, picks, noticeBox(), body);
  }
  load();
  if (updates === null) loadUpdates(false);
  iconUses.clear();
  const mine = S.snap.plugins || [];
  const sources = new Set(mine.map((p) => (p.source || "").toLowerCase()).filter(Boolean));
  // the engines' catalogs (Claude Code, Codex) are not in the shared list: their plugins have a section of their own
  const entries = (mp?.entries || []).filter((e) => !engineCat(e.catalog)).map((e) => ({ ...e, mp: true, cat: e.catalog }));
  // by link: if the same repo is pinned in a connected catalog — one row, from the catalog (an unpinned entry cannot
  // be installed from the page — the row with code review stays)
  const fromMp = new Set(entries.filter((e) => e.pinned).map((e) => (e.repo || "").toLowerCase()).filter(Boolean));
  // and for the installed ones' filter; rows without link come only from a server of an earlier release (until its restart)
  for (const p of available || []) p.cat = p.link ? "" : author(p);
  const old = (available || []).filter((p) => !sources.has((p.repo || "").toLowerCase()) && !fromMp.has((p.repo || "").toLowerCase()));
  const rest = [...entries.filter((e) => !e.installed || e.type === "module"), ...old];
  // filters: category and kind — always; catalog — when there is more than one
  const cats = [...new Set([...(mp?.catalogs || []).filter((c) => !engineCat(c.name)).map((c) => c.name),
    ...rest.map((p) => p.cat).filter(Boolean)])].sort();
  if (catBy !== "all" && !cats.includes(catBy)) catBy = "all";
  picks.replaceChildren(select("plugin-category", t("web.plugins.category_label"), [["all", t("web.plugins.category_all")], ...CATEGORIES],
    categoryBy, (v) => { categoryBy = v; }), select("plugin-kind", t("web.plugins.kind_label"), [["all", t("web.plugins.kind_all")], ...GROUPS], kindBy, (v) => { kindBy = v; }),
    ...(cats.length > 1 ? [select("plugin-cat", t("web.plugins.catalog_label"), [["all", t("web.plugins.catalog_all")], ...cats.map((c) => [c, c])], catBy, (v) => { catBy = v; })] : []));
  const parts = [el("div", "plugin-link-slot"), el("h3", "plugin-section", t("web.plugins.installed_head", { count: mine.length }))];
  if (mine.length) { const list = el("div", "plugin-list"); list.append(...mine.map(installedRow)); parts.push(list); }
  else parts.push(el("p", "hint", t("web.plugins.none_installed")));
  parts.push(...engineBlock());
  const head = el("div", "plugin-section-row");
  const ready = available && mp;
  head.append(el("h3", "plugin-section", ready ? t("web.plugins.available_head", { count: rest.length }) : t("web.plugins.available")));
  if (ready && rest.length > 1) {
    head.append(select("plugin-order", t("web.plugins.sort_label"), [["popular", t("web.plugins.sort_popular")], ["name", t("web.plugins.sort_name")]], sortBy, (v) => {
      sortBy = v;
      try { localStorage.setItem(SORT, sortBy); } catch { /* unavailable */ }
      pluginsSection();
    }));
  }
  parts.push(head);
  if (!ready) parts.push(el("p", "hint", t("web.plugins.loading")));
  else if (!rest.length) parts.push(el("p", "hint", loadError ? t("web.plugins.load_failed", { error: loadError }) :
    t("web.plugins.empty_catalog")));
  else {
    // groups by category — as compact rows, not a column of identical cards
    const box = el("div", "plugin-catalog");
    for (const [category, label] of CATEGORIES) {
      const items = sorted(rest.filter((p) => categoryOf(p) === category));
      if (!items.length) continue;
      const g = el("section", "plugin-group");
      const list = el("div", "plugin-list");
      list.append(...items.map(catalogRow));
      g.append(el("h4", "plugin-group-head", `${label} · ${items.length}`), list);
      box.append(g);
    }
    parts.push(box);
  }
  const none = el("p", "hint plugin-none", t("web.plugins.no_match"));
  none.hidden = true;
  parts.push(none);
  if (mp && !mp.none) parts.push(...marketsBlock());
  parts.push(el("p", "hint plugin-foot", t("web.plugins.footer", { api: S.snap.plugin_api || "1.0" })));
  body.replaceChildren(...parts);
  filter(body);
  return wrap;
}
