// "Files": address #/<project>/<path>. A folder is a list on the left, a file is on the right: markdown rendered nicely, html as a page (iframe
// in a sandbox, /render), images, video, audio and pdf as in the viewer (/raw, video with seeking), anything else as text.
// Every file has "Download"; close with the cross, Esc or the browser's "back"; left/right arrows move between the folder's files (08.10). View: "Page | Code", "Render | Source" (?view=). "Edit" opens a textarea, Cmd+S/Ctrl+S saves; if the file changed meanwhile, we do not overwrite it.
import { $, el, icon } from "/web/js/dom.js";  // icons come from /static/icons.svg of "Files"
import { md } from "/web/js/markdown.js";
import { locale, t } from "/web/js/i18n.js";
import { langOf, numbered } from "/web/js/code.js";
import { attach, detach, init as initComments } from "./comments.js";
import { play, textOf } from "./magic.js";


const S = { root: null, path: "", file: null, editing: false, dirty: false, siblings: [] };
const MD = /\.(md|markdown)$/i;
const HTML = /\.html?$/i;
// html and markdown have two views: rendered (page) and source (src); each has its own labels
const VIEWS = { html: [["page", t("files.view_page")], ["src", t("files.view_code")]],
  md: [["page", t("files.view_render")], ["src", t("files.view_source")]] };
const kindOf = (rel) => (HTML.test(rel) ? "html" : MD.test(rel) ? "md" : null);
// sandbox without "same origin": page scripts run with an opaque origin and cannot reach the API or storage of "Files"
const SANDBOX = "allow-scripts allow-popups allow-forms";
const HEIC = /\.hei[cf]$/i;  // iPhone photos: Chrome does not render them, so the server serves a jpeg copy (?as=jpeg)
const KIND_ICON = { image: "image", video: "video", audio: "audio", pdf: "pdf" };
const enc = encodeURIComponent;
const rawUrl = (root, rel, extra = "") => `/raw?root=${enc(root)}&path=${enc(rel)}${extra}`;
const parentOf = (path) => path.split("/").slice(0, -1).join("/");
const onThisMachine = ["127.0.0.1", "localhost"].includes(location.hostname);
// the file manager's name on the button by the id platforms.*.file_manager gives; "explorer" is translated, an unknown one is "Folder"
const MANAGERS = { finder: "Finder", files: "Files", dolphin: "Dolphin", thunar: "Thunar", nemo: "Nemo", pcmanfm: "PCManFM", caja: "Caja" };
const onMac = /Mac/.test(navigator.platform) && onThisMachine;

async function get(url) {
  const r = await fetch(url);
  return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
}

function banner(text, err) {
  const b = $("banner");
  b.textContent = text;
  b.className = "fbanner on" + (err ? " err" : "");
  clearTimeout(banner.t);
  banner.t = setTimeout(() => (b.className = "fbanner"), err ? 8000 : 2500);
}

const href = (root, path) => "#/" + encodeURIComponent(root) + (path ? "/" + path.split("/").map(encodeURIComponent).join("/") : "");

function readHash() {
  // #/project/path?line=N: jump to line N (30.09: `message-history.ts:89`, "jump to line")
  const [hash, query] = location.hash.replace(/^#\/?/, "").split("?");
  const parts = hash.split("/").filter(Boolean).map(decodeURIComponent);
  const qs = new URLSearchParams(query || "");
  const line = Number(qs.get("line")) || null;
  // session: the session the file was opened from; comments go to it (01.10)
  const view = ["page", "src"].includes(qs.get("view")) ? qs.get("view") : null;
  // embed: inside the embedded window of the hub page (right panel, 02.10): no folder list; chrome=hub — and the page draws
  // the window header itself (28.10, embedTell below)
  const embed = qs.get("embed") === "1";
  return { root: parts[0] || null, path: parts.slice(1).join("/"), line, session: qs.get("session"), view, embed,
    chrome: embed && qs.get("chrome") === "hub" };
}

// file view: from the address (?view=), otherwise the last one chosen for this type; with a line, always the source
function viewOf(f) {
  const kind = kindOf(f.rel);
  if (!kind) return null;
  if (S.line) return "src";
  if (S.view) return S.view;
  try { return localStorage.getItem("files.view." + kind) === "src" ? "src" : "page"; } catch { return "page"; }
}

function setView(v) {
  const kind = kindOf(S.file.rel);
  S.view = v;
  S.line = null;
  try { localStorage.setItem("files.view." + kind, v); } catch { /* no storage */ }
  const [hash, query] = location.hash.split("?");
  const qs = new URLSearchParams(query || "");
  qs.delete("line");
  qs.set("view", v);
  history.replaceState(null, "", hash + "?" + qs);  // no hashchange: the file is not re-read
  renderFile();
}

function viewSwitch(f) {
  const kind = kindOf(f.rel), now = viewOf(f);
  const seg = el("div", "seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", t("files.view_label"));
  for (const [v, label] of VIEWS[kind]) {
    const b = el("button", null, label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(v === now));
    b.addEventListener("click", () => { if (v !== viewOf(S.file)) setView(v); });
    seg.append(b);
  }
  return seg;
}

const renderUrl = (root, rel, mtime) => "/render/" + encodeURIComponent(root) + "/" + rel.split("/").map(encodeURIComponent).join("/")
  + (mtime ? `?v=${mtime}` : "");

function crumbs() {
  const c = $("crumbs");
  c.replaceChildren();
  const home = el("a", null, t("files.home"));
  home.href = "#/";
  c.append(home);
  if (!S.root) return;
  const parts = S.path ? S.path.split("/") : [];
  const steps = [[S.root, ""], ...parts.map((p, i) => [p, parts.slice(0, i + 1).join("/")])];
  steps.forEach(([name, path], i) => {
    c.append(el("span", "sep", "/"));
    if (i === steps.length - 1) c.append(el("span", "here", name));
    else { const a = el("a", null, name); a.href = href(S.root, path); c.append(a); }
  });
}

function actions() {
  const a = $("acts");
  a.replaceChildren();
  const f = S.file;
  if (f && kindOf(f.rel) && !S.editing && (f.text != null || kindOf(f.rel) === "html")) a.append(viewSwitch(f));
  if (f && f.text != null) {
    if (S.editing) {
      const save = el("button", "btn primary", t("files.save"));
      save.type = "button";
      save.addEventListener("click", saveFile);
      const cancel = el("button", "btn", t("files.cancel"));
      cancel.type = "button";
      cancel.addEventListener("click", cancelEdit);
      a.append(save, cancel);
    } else {
      const edit = el("button", "btn", t("files.edit"));
      edit.type = "button";
      edit.addEventListener("click", () => { S.editing = true; renderFile(); });
      a.append(edit);
    }
  }
  if (f && !S.editing) {
    // neighbouring files of the folder: < > and the left/right arrow keys
    const at = S.siblings.indexOf(f.rel);
    for (const [step, label, cls] of [[-1, t("files.prev_file"), "prev"], [1, t("files.next_file"), "next"]]) {
      const other = S.siblings[at + step];
      const b = el("a", `btn quiet icon-only ${cls}`);
      b.append(icon("back"));
      b.title = b.ariaLabel = label;
      if (at >= 0 && other) b.href = href(S.root, other); else b.setAttribute("aria-disabled", "true");
      a.append(b);
    }
  }
  if (f) {
    const dl = el("a", "btn download");
    dl.href = rawUrl(S.root, f.rel, "&download=1");
    dl.download = f.rel.split("/").pop();
    dl.title = t("files.download_file");
    dl.append(icon("download"), t("files.download"));
    a.append(dl);
  }
  // show the file in its folder: the window opens on the hub machine's screen, so the button is only for a page opened
  // on that machine and only where it has a desktop; the server tells the file manager's id in the page (data-reveal)
  const manager = onThisMachine ? document.documentElement.dataset.reveal : "";
  if (f && manager) {
    const reveal = el("button", "btn", manager === "explorer" ? t("files.reveal_explorer") : MANAGERS[manager] || t("files.reveal_folder"));
    reveal.type = "button";
    reveal.title = t(manager === "finder" ? "files.reveal_in_finder" : "files.reveal_open_folder");
    reveal.addEventListener("click", async () => {
      const r = await fetch("/api/reveal", { method: "POST", headers: { "Content-Type": "application/json", "X-Hub": "1" },
        body: JSON.stringify({ root: S.root, path: f.rel }) });
      if (!r.ok) banner((await r.json().catch(() => ({}))).error || t("files.not_opened"), true);
    });
    a.append(reveal);
  }
  if (f && onMac) {
    const vs = el("a", "btn quiet", "VS Code");
    vs.href = "vscode://file" + f.path + (S.line ? `:${S.line}` : "");  // and in VS Code, at the same line
    a.append(vs);
  }
  const hub = el("a", "btn quiet", t("files.hub"));
  hub.href = `${location.protocol}//${location.hostname}:${document.documentElement.dataset.hubPort}/`;  // set by the server
  a.append(hub);
  if (f) {  // close the file: go to its folder (and Esc)
    const x = el("a", "btn quiet icon-only close");
    x.href = href(S.root, parentOf(f.rel));
    x.title = x.ariaLabel = t("files.close_file");
    x.append(icon("close"));
    a.append(x);
  }
  if (S.chrome) embedTell();
}

function cancelEdit() {
  if (!S.dirty || confirm(t("files.leave_unsaved_edit"))) { S.editing = S.dirty = false; renderFile(); }
}

// The hub page draws the header of its embedded window (embed=1&chrome=hub, md-panel 28.10: one header for every panel):
// here the own header is hidden (files.css [data-chrome]), and the page is told the file, its path and the actions. The
// page answers with an action id from that list, nothing else is read from its message: a path or an address in it is
// not taken. The list is navigation and reading only (crumbs, view, previous/next file, "edit" = the edit mode of the
// window itself); nothing that writes a file or starts a program is on it. Messages go only to the hub page's origin (same host, its port) and are taken only from it, from the
// parent window — a report page inside this window (its sandbox, an opaque origin) cannot command it.
const HUB = `${location.protocol}//${location.hostname}:${document.documentElement.dataset.hubPort}`;
let offered = new Map();   // action id -> what it does, as last told to the page

function embedTell() {
  const f = S.file, actions = [], next = new Map();
  const offer = (id, label, run, more = {}) => { if (run) next.set(id, run); actions.push({ id, label, ...more }); };
  // the path: folders above the file are crumbs, the file (or the folder itself) is the title
  const parts = S.path ? S.path.split("/") : [];
  const steps = S.root ? [[S.root, ""], ...parts.map((p, i) => [p, parts.slice(0, i + 1).join("/")])] : [];
  const crumbs = [{ id: "crumb:home", label: t("files.home") }];
  next.set("crumb:home", () => { location.hash = "#/"; });
  steps.slice(0, -1).forEach(([name, path], i) => {
    crumbs.push({ id: `crumb:${i}`, label: name });
    next.set(`crumb:${i}`, () => { location.hash = href(S.root, path); });
  });
  const title = steps.length ? steps.at(-1)[0] : t("files.home");
  const kind = f && kindOf(f.rel);
  if (kind && !S.editing && (f.text != null || kind === "html")) {
    for (const [v, label] of VIEWS[kind]) offer(`view:${v}`, label, () => setView(v), { on: viewOf(f) === v });
  }
  // "edit" only switches the window into its edit mode; Save and Cancel are the buttons under the field, inside the window
  if (f && f.text != null && !S.editing) offer("edit", t("files.edit"), () => { S.editing = true; renderFile(); });
  if (f && !S.editing) {
    const at = S.siblings.indexOf(f.rel);
    for (const [step, id, label] of [[-1, "prev", t("files.prev_file")], [1, "next", t("files.next_file")]]) {
      const other = at >= 0 ? S.siblings[at + step] : null;
      offer(id, label, other ? () => { location.hash = href(S.root, other); } : null, { off: !other });
    }
  }
  // a download and VS Code are links the page opens itself, on the owner's click (a browser lets a frame do neither
  // without one)
  if (f) offer("download", t("files.download_file"), null, { href: new URL(rawUrl(S.root, f.rel, "&download=1"), location.href).href });
  if (f && onMac) offer("vscode", t("files.open_vscode"), null, { href: "vscode://file" + f.path + (S.line ? `:${S.line}` : "") });
  offered = next;
  const info = f ? { path: f.path, size: size(f.size), changed: new Date(f.mtime * 1000).toLocaleString(locale) } : null;
  window.parent.postMessage({ type: "files:state", title, crumbs, actions, editing: S.editing, info }, HUB);
}

window.addEventListener("message", (e) => {
  if (!S.chrome || e.source !== window.parent || e.origin !== HUB) return;
  const id = e.data && e.data.type === "files:do" && typeof e.data.id === "string" ? e.data.id : null;
  if (id && offered.has(id)) offered.get(id)();
});

// close the file: go to its folder; the browser's "back" works too: file and folder are different addresses
function closeFile() {
  if (!S.file) return;
  location.hash = href(S.root, parentOf(S.file.rel));
}

async function showList(root, dir, current) {
  const box = $("list");
  if (!root) {
    const r = await get("/api/roots");
    box.replaceChildren(...(r.data.length ? r.data.map((x) => entry(x.name, true, href(x.name, "")))
      : [el("p", "hint", t("files.no_roots"))]));
    return;
  }
  const r = await get(`/api/list?root=${encodeURIComponent(root)}&path=${encodeURIComponent(dir)}`);
  if (!r.ok) { box.replaceChildren(el("p", "hint", r.data.error || t("files.folder_missing"))); return; }
  const items = [];
  if (dir) items.push(entry("..", true, href(root, dir.split("/").slice(0, -1).join("/"))));
  S.siblings = r.data.entries.filter((e) => !e.dir).map((e) => (dir ? `${dir}/${e.name}` : e.name));
  for (const e of r.data.entries) {
    const path = dir ? `${dir}/${e.name}` : e.name;
    const a = entry(e.name, e.dir, href(root, path), e.kind);
    if (path === current) a.setAttribute("aria-current", "true");
    items.push(a);
  }
  if (!r.data.entries.length) items.push(el("p", "hint", t("files.empty")));
  box.replaceChildren(...items);
}

function entry(name, dir, link, kind) {
  const a = el("a", dir ? "dir" : "file");
  a.href = link;
  const k = el("span", "k");
  k.append(icon(dir ? (name === ".." ? "back" : "dir") : KIND_ICON[kind] || "file"));
  a.append(k, el("span", "n", name + (dir && name !== ".." ? "/" : "")));
  return a;
}

// address of an image or video from a markdown document: a relative one resolves from the document's folder, via /raw; foreign sites are not allowed
function mediaOf(src) {
  if (/^data:image\//i.test(src)) return src;
  if (/^[a-z][\w+.-]*:/i.test(src) || src.startsWith("//")) return null;
  if (src.startsWith("/")) return `/raw?path=${enc(decodeURI(src))}`;
  const parts = parentOf(S.file.rel).split("/").filter(Boolean);
  for (const p of decodeURI(src.split(/[?#]/)[0]).split("/")) {
    if (p === "..") parts.pop(); else if (p && p !== ".") parts.push(p);
  }
  return rawUrl(S.root, parts.join("/"));
}

// image, video, audio, pdf as in the viewer; if it did not show (codec, HEIC without sips), say so and offer a download
function mediaView(v, f) {
  const src = rawUrl(S.root, f.rel, f.kind === "image" && HEIC.test(f.rel) ? "&as=jpeg" : "");
  const stage = el("div", "stage");
  let node;
  if (f.kind === "image") {
    node = el("img");
    node.alt = f.rel.split("/").pop();
    node.title = t("files.zoom_title");
    node.addEventListener("click", () => stage.classList.toggle("zoom"));
  } else if (f.kind === "pdf") {
    node = el("iframe", "pdf");  // not .frame: an html page is re-read by mtime, pdf is not
    node.title = f.rel;
  } else {
    node = el(f.kind);  // video | audio
    node.controls = true;
    node.preload = "metadata";
    node.playsInline = true;
  }
  node.src = src;
  node.addEventListener("error", () => stage.replaceWith(cantShow(f, t(f.kind === "image" ? "files.cant_image"
    : "files.cant_play"))), { once: true });
  stage.append(node);
  v.dataset.mode = f.kind;
  v.append(stage);
}

function cantShow(f, why) {
  const box = el("div", "nothing");
  box.append(el("p", null, t("files.not_shown", { why })));
  const dl = el("a", "btn primary");
  dl.href = rawUrl(S.root, f.rel, "&download=1");
  dl.download = f.rel.split("/").pop();
  dl.append(icon("download"), t("files.download_size", { size: size(f.size) }));
  box.append(dl);
  return box;
}

const size = (n) => (n < 1024 ? t("files.size_b", { n }) : n < 1024 ** 2 ? t("files.size_kb", { n: Math.round(n / 1024) })
  : n < 1024 ** 3 ? t("files.size_mb", { n: (n / 1024 ** 2).toFixed(1) }) : t("files.size_gb", { n: (n / 1024 ** 3).toFixed(1) }));

function renderFile() {
  const v = $("view"), f = S.file;
  actions();
  v.replaceChildren();
  v.dataset.mode = "";  // window view for files.css: doc, code, frame, edit, image, video, audio, pdf
  if (!f) { v.append(el("p", "empty", t(S.root ? "files.pick_file" : "files.pick_folder"))); detach(); return; }
  v.append(meta(f));
  if (f.spec && !S.editing) v.append(specBar(f));   // an executor's spec: "Accept" / "Drop" (core/specmode.py)
  if (S.editing) {
    v.dataset.mode = "edit";
    const ta = el("textarea");
    ta.value = f.draft ?? f.text;
    ta.spellcheck = false;
    ta.addEventListener("input", () => { S.dirty = true; f.draft = ta.value; });
    v.append(ta);
    if (S.chrome) {   // the hub page's header has no buttons in a row: "Save" and "Cancel" under the field
      const bar = el("div", "ebar"), save = el("button", "btn primary", t("files.save")), cancel = el("button", "btn", t("files.cancel"));
      save.type = cancel.type = "button";
      save.addEventListener("click", saveFile);
      cancel.addEventListener("click", cancelEdit);
      bar.append(cancel, save);
      v.append(bar);
    }
    ta.focus();
    detach();
    return;
  }
  let content = null;
  const view = viewOf(f);
  if (kindOf(f.rel) === "html" && view === "page") {
    // html as a page: neighbouring css and images by relative links through the same /render
    const fr = el("iframe", "frame");
    fr.setAttribute("sandbox", SANDBOX);
    fr.title = f.rel;
    fr.src = renderUrl(S.root, f.rel, f.mtime);
    v.dataset.mode = "frame";
    v.append(fr);
    detach();  // selection is not visible in a foreign document, so comments are on the "Code" view
    return;
  }
  if (["image", "video", "audio", "pdf"].includes(f.kind)) {
    mediaView(v, f);
  } else if (f.text == null) {
    v.append(cantShow(f, f.why || t("files.not_text")));
  } else if (MD.test(f.rel) && view === "page") {  // for a markdown line, the source with numbers: the rendered view has no lines
    // documents are laid out by width: a wrap is not a new line; the document's own images are visible (media)
    v.append(content = md(f.text, { breaks: false, media: mediaOf }));
    content.classList.add("doc");
    v.dataset.mode = "doc";
    const col = el("div", "doccol");  // document column: the line above it has the same width, centered in the window
    col.append(v.querySelector(".meta"), content);
    v.append(col);
  } else {
    // code with highlighting by the file's language and line numbers (30.09)
    v.append(content = numbered(f.text, langOf(f.rel, f.text), null, "src"));
    v.dataset.mode = "code";
  }
  // select a piece to comment on it; comments go to the session so it rewrites (comments.js)
  if (content) v.querySelector(".meta").append(el("span", "chint", t(S.chrome ? "files.select_to_comment" : "files.comment_hint")));
  if (content) attach({ root: S.root, rel: f.rel, abs: f.path, content, session: S.session }); else detach();
}

// the strip of an executor's spec: open — who writes it, "Accept" (the text on screen: its sum goes along) and "Drop";
// accepted — when and by whom, and whether the file changed since (the accepted copy next to it, to compare)
function specBar(f) {
  const x = f.spec, bar = el("div", "specbar");
  bar.setAttribute("role", "status");
  const when = x.at ? new Date(x.at * 1000).toLocaleString(locale, { dateStyle: "short", timeStyle: "short" }) : "";
  if (x.copy) {
    bar.append(el("span", null, t("files.spec_copy", { id: x.task })));
  } else if (x.state === "open") {
    bar.append(el("span", null, t(x.ready ? "files.spec_ready" : "files.spec_writing", { id: x.task, name: x.executor })));
    if (!x.ready) return bar;   // the executor is in a turn (the discussion goes on): no buttons until it has reported
    const yes = el("button", "btn primary small", t("files.spec_accept")), no = el("button", "btn small", t("files.spec_drop"));
    yes.type = no.type = "button";
    const act = async (action, b) => {
      b.disabled = true;
      const r = await fetch("/api/spec", { method: "POST", headers: { "Content-Type": "application/json", "X-Hub": "1" },
        body: JSON.stringify({ delegation: x.task, action }) });
      const d = await r.json().catch(() => ({}));
      b.disabled = false;
      if (!r.ok) { banner(d.error || t("files.spec_not_sent"), true); return; }
      banner(t(action === "accept" ? "files.spec_accepted_now" : "files.spec_dropped_now", { id: x.task }));
      setTimeout(route, 800);   // the core records it in a moment: the strip shows "Accepted"
    };
    yes.addEventListener("click", () => act("accept", yes));
    no.addEventListener("click", () => act("drop", no));
    bar.append(yes, no);
  } else if (x.state === "accepted") {
    bar.append(el("span", null, t("files.spec_accepted", { id: x.task, when, who: x.by || "" })));
    if (x.changed) {
      const a = el("a", null, t("files.spec_changed_since"));
      a.href = x.accepted_href;
      bar.append(" · ", a);
    }
  } else {
    bar.append(el("span", null, t("files.spec_dropped", { id: x.task })));
  }
  return bar;
}

// line above the file: path (mono) · size · when it changed
function meta(f) {
  const m = el("div", "meta");
  if (S.chrome) return m;   // path, size and date are in the hub page's menu; here only the comment hint
  m.append(el("span", "path", f.path), ` · ${size(f.size)} · ${new Date(f.mtime * 1000).toLocaleString(locale)}`);
  return m;
}

async function saveFile() {
  const f = S.file;
  const r = await fetch("/api/write", { method: "POST", headers: { "Content-Type": "application/json", "X-Hub": "1" },
    body: JSON.stringify({ root: S.root, path: f.rel, text: f.draft ?? f.text, sha: f.sha }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { banner(d.error || t("files.not_saved"), true); return; }
  f.text = f.draft ?? f.text;
  f.sha = d.sha;
  f.mtime = d.mtime;
  delete f.draft;
  S.editing = S.dirty = false;
  renderFile();
  banner(t("files.saved"));
}

async function route() {
  if (S.dirty && !confirm(t("files.leave_unsaved"))) return;
  S.editing = S.dirty = false;
  offered = new Map();   // the previous file's actions do not run while the next one loads (embedTell offers anew)
  const { root, path, line, session, view, embed, chrome } = readHash();
  S.view = view;
  if (embed) $("app").dataset.embed = "1";  // stays for the whole visit: links inside a file open in the same window
  if (chrome) S.chrome = $("app").dataset.chrome = "hub";   // for the whole visit too, folders included
  S.root = root;
  S.session = session;
  S.path = path;
  S.line = line;
  S.file = null;
  crumbs();
  // a folder (path above the file, cross, Esc, a link in the document) in the embedded window leaves embed: "file only"
  // hid the folder list, and the folder opened as an empty panel
  if (!root) { delete $("app").dataset.embed; $("app").dataset.view = "list"; await showList(null); renderFile(); return; }
  const dir = await get(`/api/list?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`);
  if (dir.ok) { delete $("app").dataset.embed; $("app").dataset.view = "list"; await showList(root, path); renderFile(); return; }
  const r = await get(`/api/read?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`);
  const parent = parentOf(path);
  await showList(root, parent, path);
  if (!r.ok) { $("view").replaceChildren(el("p", "hint", r.data.error || t("files.file_missing"))); actions(); return; }
  S.file = r.data;
  $("app").dataset.view = "file";
  renderFile();
  goToLine(S.line);
}

// to a line: scroll to it and highlight it (code lines are span.ln in order, code.js numbered)
function goToLine(n) {
  const ln = n && $("view").querySelectorAll("pre.numbered .ln")[n - 1];
  if (!ln) return;
  ln.classList.add("hit");
  ln.scrollIntoView({ block: "center" });
}

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (e) => { if (S.dirty) e.preventDefault(); });
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s" && S.editing) { e.preventDefault(); saveFile(); }
  // Esc closes the file, left/right arrows go to a neighbour; not in an input field, not while editing, not in the player (arrows seek there)
  const t = e.target;
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || S.editing || !S.file
    || t.closest?.("input, textarea, select, video, audio, [contenteditable], .ccompose")) return;
  if (e.key === "Escape") {
    const stage = $("view").querySelector(".stage.zoom");
    if (stage) stage.classList.remove("zoom");
    else if (S.chrome) window.parent.postMessage({ type: "files:close" }, HUB);   // the hub page closes its panel
    else closeFile();
  } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    const back = (e.key === "ArrowLeft") !== (document.documentElement.dir === "rtl");   // the arrows point at places on the screen
    const other = S.siblings[S.siblings.indexOf(S.file.rel) + (back ? -1 : 1)];
    if (other && S.siblings.includes(S.file.rel)) location.hash = href(S.root, other);
  }
});
// the file changed on disk (a session rewrote it from comments): re-read it; while editing, only say so
setInterval(async () => {
  const f = S.file;
  const frame = $("view").querySelector("iframe.frame");
  if (!f || (f.text == null && !frame) || document.hidden) return;
  const r = await get(`/api/stat?root=${encodeURIComponent(S.root)}&path=${encodeURIComponent(f.rel)}`);
  if (r.ok && S.file === f && f.spec?.state === "open" && !f.spec.copy && "spec_ready" in r.data
      && r.data.spec_ready !== f.spec.ready) {   // the executor's turn began or ended: the strip's buttons go or come
    f.spec.ready = r.data.spec_ready;
    $("view").querySelector(".specbar")?.replaceWith(specBar(f));
  }
  if (!r.ok || r.data.mtime === f.mtime || S.file !== f) return;
  if (S.editing) { if (!f.warned) { f.warned = true; banner(t("files.changed_on_disk"), true); } return; }
  const n = await get(`/api/read?root=${encodeURIComponent(S.root)}&path=${encodeURIComponent(f.rel)}`);
  if (!n.ok || S.file !== f) return;
  if (frame && frame.isConnected) {  // a page is just reloaded, without the "magic": the document text is not ours
    S.file = n.data;
    frame.src = renderUrl(S.root, n.data.rel, n.data.mtime);
    $("view").querySelector(".meta")?.replaceWith(meta(n.data));
    banner(t("files.page_updated"));
    return;
  }
  const top = $("view").scrollTop;
  const before = textOf($("view").querySelector(".md, pre.src"));
  S.file = n.data;
  renderFile();
  $("view").scrollTop = top;
  // rewritten before the reader's eyes: deleted text collapses, added text fades in (magic.js), then the clean view
  const now = $("view").querySelector(".md, pre.src");
  if (!now || !before) { banner(t("files.file_updated")); return; }
  banner(t("files.rewriting"));
  play(now, before, ({ added, removed }) => {
    if (S.file !== n.data || S.editing) return;
    const keep = $("view").scrollTop;
    renderFile();
    $("view").scrollTop = keep;
    banner(added || removed ? t("files.rewritten", { added: t("files.added_words", { count: added }),
      removed: t("files.removed_words", { count: removed }) }) : t("files.file_updated"));
  });
}, 4000);

initComments(banner);
route();
