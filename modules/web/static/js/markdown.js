// Markdown of model replies: marked -> DOMPurify. Without the libraries — plain text.
import { el, icon } from "./dom.js";
import { S, renderLive } from "./state.js";
import { highlightMd } from "./code.js";
import { t, bare } from "./i18n.js";

/* global marked, DOMPurify, hljs */
const ok = typeof marked !== "undefined" && typeof DOMPurify !== "undefined";
if (ok) {
  marked.setOptions({ gfm: true, breaks: true });
  // [plan](file:///abs/plan.md): the sanitizer drops a file: href (an http page cannot follow it anyway), so its address
  // is kept aside here and linkHrefPaths treats it like an absolute path; where nothing does (Files, boards) the link
  // stays without an address, as before
  DOMPurify.addHook("beforeSanitizeAttributes", (n) => {
    const href = n.tagName === "A" && n.getAttribute("href");
    if (href && /^file:\/\/\//i.test(href)) n.setAttribute("data-file-href", href);
  });
  DOMPurify.addHook("afterSanitizeAttributes", (n) => {
    if (n.tagName === "A") { n.target = "_blank"; n.rel = "noopener noreferrer"; }
  });
}

// the model's images, forms and styles are not needed, and a foreign image can track who opened the page. Media load
// their URL without a click, style and srcset forge cards and pull URLs, and an id or name from the text shadows a
// global name of the page (id="mermaid" hid the diagram library) — DOMPurify prefixes them
const MEDIA = ["video", "audio", "source", "track", "picture"];
const FORBID = ["img", "style", "form", "input", "button", "iframe", "svg", "math", ...MEDIA];
// data-* from the text are dropped as well: the page finds its own nodes by them (data-key is the feed's scroll anchor,
// data-next and data-conn-at are rewritten by tickers). data-file-href is the hook's above: an added attribute has its
// value checked as an address, which file: does not pass — so it is named URI-safe too
const CLEAN = { USE_PROFILES: { html: true }, FORBID_ATTR: ["style", "srcset"], SANITIZE_NAMED_PROPS: true,
  ALLOW_DATA_ATTR: false, ADD_ATTR: ["data-file-href"], ADD_URI_SAFE_ATTR: ["data-file-href"] };
// a class from the text would borrow the page's own look and layers (class="lightbox" covered the whole page with a
// fake viewer): only marked's own one stays — language-* on a code block, read by the highlighter and the diagrams.
// The page's classes (hljs, path-wait, tbl, mermaid-box) are put on after this
function textClasses(root) {
  for (const n of root.querySelectorAll("[class]")) {
    const keep = n.tagName === "CODE" ? [...n.classList].filter((c) => /^language-[\w+#.-]+$/.test(c)) : [];
    if (keep.length) n.className = keep.join(" "); else n.removeAttribute("class");
  }
}

// `path` → a link into the Files module (so a path the model mentions can be opened).
// A relative path is resolved by Files itself from the session folder and its parents (modules/files/paths.py: joining
// here doubled `proj-wt2/…`), `file:line` points to the file. Files is the same host, port 8788 (also on the tailnet).
const PATHLIKE = /^(?:[\w.~/-]*[\w-]\.[A-Za-z0-9]{1,8}|[\w.~-]*\/[\w./-]+)$/;
// folders with a space in an absolute path (`My Projects`, `Application Support`): the word after the space starts with
// a capital or a digit, otherwise it is a phrase ("/tmp/x and y") or a command (`/usr/bin/ls -la`)
const SPACED = String.raw`[\w.@+-]+(?: [A-Z0-9][\w.@+-]*)*`;
const ABS_CODE = new RegExp(String.raw`^~?(?:\/${SPACED}){2,}$`);
export function fileLink(path, cwd, line, session) {
  const a = document.createElement("a");
  const from = path.startsWith("/") || path.startsWith("~") ? "" : `&cwd=${encodeURIComponent(cwd)}`;
  a.href = `${location.protocol}//${location.hostname}:${S.snap?.files_port || 8788}/open?path=${encodeURIComponent(path)}${from}`
    + (line ? `&line=${line}` : "") + (session ? `&session=${encodeURIComponent(session)}` : "");  // `file:89` — to line 89
  a.target = "_blank";
  a.rel = "noopener";
  a.title = t("web.markdown.open_in_files", { path: `${path}${line ? ":" + line : ""}` });
  return a;
}

// where a path leads is decided by the server (/api/paths, modules/files/paths.py:resolve): a link only for what opens
// (earlier, links often led to files that could not be accessed, and "hello.com" without http was highlighted as a file).
// The answer is remembered: the page redraws the feed on every snapshot; "not found" is cached for a minute (the file
// may get created). While there is no answer the text sits in span.path-wait; once it arrives, a link takes its place
const where = new Map();   // path, folder, hint → {open: "#/root/path"} | {web: "https://…"} | {} and when
const asked = new Map();
let asking = null;
const MISS_MS = 60000;
function filesHref(hash, line, session) {
  // "155-166" (a range in the text) — open at its start, not the whole range, which Files does not understand
  const at = line ? String(line).split("-")[0] : "";
  const q = [at ? `line=${at}` : "", session ? `session=${encodeURIComponent(session)}` : ""].filter(Boolean);
  return `${location.protocol}//${location.hostname}:${S.snap?.files_port || 8788}/${hash}${q.length ? "?" + q.join("&") : ""}`;
}
function refLink(got, data) {
  if (!got.open && !got.web && !got.file) return null;
  const a = document.createElement("a");
  // file — an image the session showed from outside the project folders (over the tailnet): the image itself, from the page
  a.href = got.open ? filesHref(got.open, data.line, data.session) : got.file || got.web;
  a.target = "_blank";
  a.rel = got.open ? "noopener" : "noopener noreferrer";
  a.title = got.open ? t("web.markdown.open_in_files", { path: `${data.path}${data.line ? ":" + data.line : ""}` }) : got.web;
  Object.assign(a.dataset, data);
  delete a.dataset.key;   // the span's wait key is not a feed row key: [data-key] is the scroll anchor (main.js)
  if (got.web) a.dataset.site = "1";   // a site (github.com/x/y), not a file — the short name leaves it alone
  if (data.stop) a.addEventListener("click", (e) => e.stopPropagation());  // inside an option label — do not select it
  return a;
}
// link text after confirmation (a is a real "A", not span.path-wait): with the checkbox on, the path at the start of
// the text is replaced by the short name — only for a file (not a site) and only if there is one (dataset.short is empty
// for a folder and for a path without an extension — see shortNames); the rest (`:12:5`) stays as written. With the
// checkbox off the text is left alone
function setDisplay(a) {
  if (!S.shortPaths || a.dataset.site || !a.dataset.short) return;
  const node = a.querySelector(":scope > code") || a, text = node.textContent, orig = a.dataset.orig || "";
  if (orig && text.startsWith(orig)) node.textContent = a.dataset.short + text.slice(orig.length);
}
async function ask() {
  asking = null;
  const batch = [...asked.entries()].slice(0, 500);
  for (const [k] of batch) asked.delete(k);
  if (asked.size) asking = setTimeout(ask, 0);
  let items = null;
  try {
    const r = await fetch("/api/paths", { method: "POST", headers: { "Content-Type": "application/json", "X-Hub": "1" },
      body: JSON.stringify({ items: batch.map(([, v]) => v) }) });
    items = r.ok ? (await r.json()).items : null;
  } catch { items = null; }
  if (!Array.isArray(items)) return;   // no connection — stays as text, we ask again on the next redraw
  batch.forEach(([k], i) => where.set(k, { ...(items[i] || {}), at: Date.now() }));
  for (const [k] of batch) for (const done of waiters.get(k) || []) done(where.get(k));
  for (const [k] of batch) waiters.delete(k);
  for (const span of document.querySelectorAll("span.path-wait")) {
    const got = where.get(span.dataset.key);
    if (!got) continue;
    const a = refLink(got, span.dataset);
    // the short name applies to a link confirmed only now too (otherwise the first async delivery would never be
    // shortened)
    if (a) { a.append(...span.childNodes); setDisplay(a); span.replaceWith(a); } else span.replaceWith(...span.childNodes);
  }
  // what was drawn before the answer was a guess: an image path got a preview, and "Brief" removed the path from the
  // text — neither is in the span any more; the redraw builds them from the answer rather than from the next snapshot
  // (a background one — waits for the user's selection and scrolling)
  renderLive();
}
// the short name applies only to a real file name: one with an extension (file name including the extension). For a
// folder (a path ending in a slash) or an executable without an extension ("bin/hub") the last segment does not look
// like a file name — the path is left alone. When short names collide within one block of text
// ("modules/web/server.py" and "modules/files/server.py" are both "server.py") one more path segment is added
// ("web/server.py") so the two are not confused
export function shortNames(paths) {
  const segs = paths.map((p) => p.split("/"));
  const base = segs.map((s) => s[s.length - 1]);   // for a folder ("conf.d/") it is empty: not a file name
  const has = base.map((b) => /\.[^./]+$/.test(b));
  const count = new Map(), seen = new Set();
  base.forEach((b, i) => {   // the same path twice (`plan.md:12` and `plan.md:30`) is not a name collision
    if (has[i] && !seen.has(paths[i])) { seen.add(paths[i]); count.set(b, (count.get(b) || 0) + 1); }
  });
  return new Map(paths.map((p, i) => [p, !has[i] ? null
    : (count.get(base[i]) > 1 && segs[i].length > 1 ? segs[i].slice(-2).join("/") : base[i])]));
}
// a link for the path, or null (already known: neither a file nor a site); not known yet — span.path-wait, which
// turns into a link by itself. short — the short name without the line (shortNames), null — do not shorten the path
// (a folder, no extension)
export function pathRef(path, cwd, line, session, hint, stop = false, short = null) {
  const key = [path, cwd || "", hint || ""].join("\n"), got = where.get(key);
  const data = { path: hint || path, line: line || "", session: session || "", stop: stop ? "1" : "",
                 orig: path, short: short || "" };
  if (got && (got.open || got.web || got.file || Date.now() - got.at < MISS_MS)) return refLink(got, data);
  const span = document.createElement("span");
  span.className = "path-wait";
  Object.assign(span.dataset, data, { key });
  if (!asked.has(key)) asked.set(key, { path, cwd: cwd || "", hint: hint || "" });
  asking ||= setTimeout(ask, 0);
  return span;
}

// where a path leads, as a promise (the same batched /api/paths and cache as pathRef): {open: "#/root/path"} for a file
// or folder "Files" opens, {} when it leads nowhere. Not answered (no connection) — it stays pending until a later ask
const waiters = new Map();
export function whereIs(path, cwd) {
  const key = [path, cwd || "", ""].join("\n"), got = where.get(key);
  if (got && (got.open || got.web || got.file || Date.now() - got.at < MISS_MS)) return Promise.resolve(got);
  return new Promise((done) => {
    waiters.set(key, [...(waiters.get(key) || []), done]);
    if (!asked.has(key)) asked.set(key, { path, cwd: cwd || "", hint: "" });
    asking ||= setTimeout(ask, 0);
  });
}

// known — absolute paths of files the session itself read and edited (`hub.json` is
// ~/.config/hub-core/hub.json, not a file in the session folder): a short path that is the tail of one leads to it
export function linkPaths(root, cwd, known = [], session = null) {
  const items = [];
  for (const code of root.querySelectorAll("code")) {
    if (code.closest("pre, a")) continue;
    const line = (code.textContent.match(/:(\d+)(?::\d+)?$/) || [])[1];
    const path = code.textContent.replace(/:\d+(:\d+)?$/, "");
    // codes and constants joined by slashes (`ACCESS_MISSING/INVALID/EXPIRED`) are not paths: a path has lowercase letters
    if (!(PATHLIKE.test(path) || ABS_CODE.test(path)) || path.includes("://") || !/[a-z]/.test(path)) continue;
    let hint = null;
    if (!path.startsWith("/") && !path.startsWith("~")) {
      const tail = "/" + path.replace(/^\.\//, "");
      hint = known.find((k) => k.endsWith(tail)) || null;   // the server uses it only while it exists
    }
    items.push({ code, path, line, hint });
  }
  const short = shortNames(items.map((i) => i.path));   // short-name collisions are checked within this block of text
  for (const { code, path, line, hint } of items) {
    const a = pathRef(path, cwd, line, session, hint, false, short.get(path));
    if (!a) continue;
    code.replaceWith(a);
    a.append(code);
    // while the path is unconfirmed (a is still span.path-wait, not a link) the text is left alone; the short name
    // (setDisplay) applies only to a confirmed link: if it is never confirmed, code.textContent stays as it was
    if (a.tagName === "A") setDisplay(a);
  }
  return root;
}

// Jira issue keys → links, a bare link to an issue → its key. The rule is the same as in modules/jira/links.py: only
// project keys from the settings (jira in hub.json, delivered in the snapshot); code only if it is entirely a key; not pre and not links.
export function linkJira(root, jira) {
  if (!jira || !jira.url || !(jira.projects || []).length) return root;
  const keys = jira.projects.map((p) => p.replace(/[^\w]/g, "")).sort((a, b) => b.length - a.length).join("|");
  const keyRe = new RegExp(`(?<![\\w/-])((?:${keys})-\\d+)(?![\\w-])`, "g");
  const whole = new RegExp(`^(?:${keys})-\\d+$`);
  const browse = (k) => `${jira.url}/browse/${k}`;
  const link = (k) => { const a = document.createElement("a"); a.href = browse(k); a.target = "_blank"; a.rel = "noopener noreferrer"; return a; };
  for (const a of root.querySelectorAll("a[href]")) {  // a bare issue link: text = URL → key
    const m = a.getAttribute("href").match(new RegExp(`^${jira.url.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/browse/((?:${keys})-\\d+)`));
    if (m && a.textContent === a.getAttribute("href")) a.textContent = m[1];
  }
  for (const code of root.querySelectorAll("code")) {
    if (code.closest("pre, a") || !whole.test(code.textContent)) continue;
    const a = link(code.textContent);
    code.replaceWith(a);
    a.append(code);
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest("a, pre, code") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT) });
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  for (const t of texts) {
    const s = t.nodeValue, parts = [];
    let last = 0;
    for (const m of s.matchAll(keyRe)) {
      parts.push(document.createTextNode(s.slice(last, m.index)));
      const a = link(m[1]);
      a.textContent = m[1];
      parts.push(a);
      last = m.index + m[1].length;
    }
    if (parts.length) t.replaceWith(...parts, document.createTextNode(s.slice(last)));
  }
  return root;
}

// `name` of an existing session → a link to its tab; the code must be entirely the name, not pre and not existing
// links; opens in the same tab
export function linkSessions(root, names) {
  const known = new Set(names || []);
  for (const code of root.querySelectorAll("code")) {
    if (code.closest("pre, a") || !known.has(code.textContent)) continue;
    const a = document.createElement("a");
    a.href = "#/s/" + encodeURIComponent(code.textContent);
    a.title = t("web.markdown.open_session");
    code.replaceWith(a);
    a.append(code);
  }
  return root;
}

// common link handling on top of markdown — files (path and code), Jira, sessions — a single source of truth for the
// feed (feed.js: mdIn) and for question/option cards (cards.js:rich), so they do not drift apart across functions
export function linkAll(node, cwd, known, session, jira, sessionNames) {
  return linkSessions(linkJira(linkTextPaths(linkPaths(linkHrefPaths(node, cwd, session), cwd, known, session), cwd, session),
    jira), sessionNames);
}

// [plan](/Users/me/docs/plan.md): the browser resolves a host-less URL against the hub page (:8787/Users/… — "no such
// page"). A link with an absolute path is treated like a path in text: it leads to Files, and if it does not open it stays text.
// [plan](file:///Users/me/docs/plan.md) is the same path: the hub opens nothing on the computer itself, Files shows it
const HREF_PATH = /^~?\/(?:Users|private|tmp|opt|var|etc|home|data|Volumes)(?:\/[^/?#]+)+$/;
export function linkHrefPaths(root, cwd, session = null) {
  for (const a of root.querySelectorAll("a[href], a[data-file-href]")) {
    // file:///abs/path (kept by the hook at the top, its href is gone): the same absolute path
    let raw = a.getAttribute("href") ?? a.dataset.fileHref.replace(/^file:\/\//i, "");
    try { raw = decodeURI(raw); } catch { /* not a URL — the check below filters it out */ }
    const path = raw.replace(/:\d+(?::\d+)?$/, "");
    if (!HREF_PATH.test(path)) continue;
    const line = (raw.match(/:(\d+)(?::\d+)?$/) || [])[1];
    const ref = pathRef(path, cwd, line, session, null, true);
    if (ref) ref.append(...a.childNodes);
    a.replaceWith(ref || document.createTextNode(a.textContent));
  }
  return root;
}

// tool output is plain text: absolute paths in it (`The file /Users/…/dock.js has been updated`) become links
const ABS = new RegExp(String.raw`(^|[\s"'\x60(=])((?:~|\/(?:Users|private|tmp|opt|var|etc|home|data|Volumes))(?:\/${SPACED}(?=\/))*(?:\/[\w.@+-]+)+)`, "g");
export function linkAbsPaths(node, session = null) {
  const nodes = [...node.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE);
  const matches = nodes.map((t) => [...t.nodeValue.matchAll(ABS)].map((m) => ({ m, path: m[2].replace(/[.,:;]+$/, "") })));
  const short = shortNames(matches.flat().map((x) => x.path));   // short-name collisions are checked within this block
  nodes.forEach((t, ti) => {
    const s = t.nodeValue, parts = [];
    let last = 0;
    for (const { m, path } of matches[ti]) {
      const at = m.index + m[1].length;
      const a = pathRef(path, null, null, session, null, false, short.get(path));
      if (!a) continue;
      parts.push(document.createTextNode(s.slice(last, at)));
      // while the path is unconfirmed (a is span.path-wait, not a link) the full text is shown; if it is never
      // confirmed it goes back as it was, untruncated; once confirmed — the short name (setDisplay), if it is a file, not a site
      a.textContent = path;
      if (a.tagName === "A") setDisplay(a);
      parts.push(a);
      last = at + path.length;
    }
    if (parts.length) t.replaceWith(...parts, document.createTextNode(s.slice(last)));
  });
  return node;
}

// a path in plain text (session questions, answer options — so specs and plans can be opened in the browser): with a
// slash and a file extension, relative to the session folder; `:N` is the line.
// A bare file name only with a "document" extension ("(inventory.md)" used to go unhighlighted); no js/ts:
// "Node.js" is not a file. The same rule is in modules/telegram/fmt.py
const BARE_EXT = "md|markdown|txt|json|jsonl|yaml|yml|toml|csv|tsv|log|pdf|html|sql|sh|py|ini|env|xml|png|jpg|jpeg|svg|webp";
// code extensions — as a bare name only together with a line number/range right after (`:155`, `:155-166`): without it
// it is indistinguishable from prose ("Node.js"), and with it it is an explicit reference to a place in code
const CODE_EXT = "ts|tsx|js|jsx|mjs|cjs|py|go|rs|java|kt|swift|c|h|cc|cpp|hpp|cs|php|rb|lua|vue";
const TEXT_PATH = new RegExp(`(^|[\\s(«"'\`])(~?(?:\\/${SPACED})+(?=\\/)\\/[\\w.@-]+\\.[A-Za-z0-9]{1,8}|(?:~|\\.{1,2})?\\/?[\\w.@-]+(?:\\/[\\w.@-]+)+\\.[A-Za-z0-9]{1,8}`
  + `|[\\w@-][\\w.@-]*\\.(?:${BARE_EXT})`
  + `|[\\w@-][\\w.@-]*\\.(?:${CODE_EXT})(?=:\\d))(?::(\\d+(?:-\\d+)?))?(?![\\w/])`, "g");
export function linkTextPaths(node, cwd, session = null) {
  // links and code are skipped whole, not text node by text node (a highlighted code block is thousands of them); a plain
  // walk, not a TreeWalker with a filter: the filter is a call into the page's code for every node of every reply
  if (node.closest?.("a, code, pre")) return node;
  const texts = [], skip = new Set(["a", "code", "pre"]);
  const walk = (n) => {
    for (let c = n.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) texts.push(c);
      else if (c.nodeType === 1 && !skip.has(c.localName)) walk(c);
    }
  };
  walk(node);
  const matches = texts.map((t) => [...t.nodeValue.matchAll(TEXT_PATH)].filter((m) =>
    // lowercase letters in the path itself, not only in the extension (ERROR/CODES/WITH_SLASHES are not paths)
    /[a-z]/.test(m[2].replace(/\.[A-Za-z0-9]+$/, "")) && (m[2].startsWith("/") || m[2].startsWith("~") || cwd)));
  const short = shortNames(matches.flat().map((m) => m[2]));   // short-name collisions are checked within this block
  texts.forEach((t, ti) => {
    const s = t.nodeValue, parts = [];
    let last = 0;
    for (const m of matches[ti]) {
      const path = m[2], at = m.index + m[1].length, end = at + path.length + (m[3] ? m[3].length + 1 : 0);
      const a = pathRef(path, cwd, m[3], session, null, true, short.get(path));   // the range "155-166" goes whole into title, and (from its start) into href
      if (!a) continue;
      parts.push(document.createTextNode(s.slice(last, at)));
      // while the path is unconfirmed (a is span.path-wait, not a link) the full text is shown; once confirmed —
      // the short name (setDisplay), if it is a file, not a site
      a.textContent = path + (m[3] ? `:${m[3]}` : "");
      if (a.tagName === "A") setDisplay(a);
      parts.push(a);
      last = end;
    }
    if (parts.length) t.replaceWith(...parts, document.createTextNode(s.slice(last)));
  });
  return node;
}

// mermaid diagrams in markdown — both in Files and in the feed. The library (vendor, 2.5 MB) is loaded only where a
// diagram exists; a finished picture comes from a cache keyed by the diagram text: the feed is rebuilt on every
// snapshot. If drawing fails, the diagram text and the reason are shown
let mermaidLoading = null, mermaidId = 0;
const mermaidDone = new Map();
function loadMermaid() {
  mermaidLoading ||= new Promise((ok, bad) => {
    if (window.mermaid) return ok(window.mermaid);
    const s = document.createElement("script");
    s.src = "/vendor/mermaid.min.js";
    s.onload = () => ok(window.mermaid);
    s.onerror = () => { mermaidLoading = null; bad(new Error(t("web.markdown.lib_not_loaded"))); };
    document.head.append(s);
  });
  return mermaidLoading;
}
// the diagram text is foreign, so mermaid's SVG is sanitized once more before it reaches the page: SVG with HTML labels
// (inside foreignObject) and formulas, without scripts, handlers or anything that loads a URL; only the root <svg>
// remains. The nodes go into the page as they are, without re-parsing a string
const SVG_CLEAN = { USE_PROFILES: { svg: true, svgFilters: true, html: true, mathMl: true }, ADD_TAGS: ["foreignObject"],
  HTML_INTEGRATION_POINTS: { foreignobject: true },
  FORBID_TAGS: ["img", "image", "iframe", "form", "input", "button", ...MEDIA], FORBID_ATTR: ["srcset"],
  RETURN_DOM_FRAGMENT: true };
function cleanSvg(svg) {
  const frag = DOMPurify.sanitize(svg, SVG_CLEAN);
  if (frag.childElementCount !== 1 || frag.firstElementChild.tagName.toLowerCase() !== "svg") {
    throw new Error(t("web.markdown.not_one_svg"));
  }
  // an HTML label is the diagram text's own markup: mermaid puts no data-* there (its own sit on the SVG nodes and its
  // styles read them), and the page finds its nodes by data-* (the feed's folds, row keys, clocks)
  for (const n of frag.firstElementChild.querySelectorAll("foreignObject *")) {
    for (const a of [...n.attributes]) if (a.name.startsWith("data-")) n.removeAttribute(a.name);
  }
  return frag.firstElementChild;
}
function mermaidBlocks(b) {
  for (const c of b.querySelectorAll("pre > code.language-mermaid")) {
    const src = c.textContent, box = el("div", "mermaid-box");
    c.parentElement.replaceWith(box);
    if (mermaidDone.has(src)) { box.append(mermaidDone.get(src).cloneNode(true)); continue; }
    box.append(el("pre", "mermaid-src", src));
    const id = `mmd${++mermaidId}`;
    loadMermaid().then(async (m) => {
      // the page's scheme, not the system's: tokens.css follows the system, a theme sets its own (Clean light on a dark system)
      const dark = /dark/.test(getComputedStyle(document.documentElement).colorScheme || "");
      // no error picture: mermaid used to draw it (a "bomb") into a temporary node in body and leave it at the bottom of the page
      m.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default", suppressErrorRendering: true });
      const svg = cleanSvg((await m.render(id, src)).svg);
      mermaidDone.set(src, svg);
      box.replaceChildren(svg.cloneNode(true));
    }).catch((e) => {
      for (const n of document.querySelectorAll(`#${id}, #d${id}`)) if (!n.closest(".mermaid-box")) n.remove();
      box.append(el("p", "hint warn", t("web.markdown.diagram_failed", { reason: String(e?.message || e).split("\n")[0] })));
    });
  }
}

// a "Copy" button on a code block (a terminal command in one tap, no mouse selection). navigator.clipboard exists only
// in a secure context (https, localhost); a page served by a home-network address is http: then selection and
// execCommand("copy") are used
function copyByHand(text) {
  const area = el("textarea", "code-copy-src"), was = document.activeElement;
  area.value = text;
  area.readOnly = true;   // iOS: no keyboard and no page zoom
  document.body.append(area);
  try {
    area.select();
    area.setSelectionRange(0, text.length);   // iOS: select() does not select
    return document.execCommand("copy");
  } catch { return false; } finally {
    area.remove();
    was?.focus?.();
  }
}
export function copyText(text) {
  text = bare(text);   // a right-to-left page isolates substituted values: not in what is copied
  const byHand = () => copyByHand(text) ? undefined : Promise.reject(new Error("copy"));
  // writeText can fail even in a secure context (tab without focus, permission denied) — then fall back to by-hand too
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text).catch(byHand);
  return byHand() || Promise.resolve();   // right in the click handler: execCommand needs a user gesture
}
function copyButtons(b) {
  for (const pre of b.querySelectorAll("pre")) {
    const code = pre.querySelector(":scope > code");
    if (!code) continue;
    const box = el("div", "code-box"), btn = el("button", "code-copy");
    btn.type = "button";
    // data-state: "copy" | "ok" | "fail" — a theme styles the result without reading which icon is inside
    const show = (sign, title, state) => { btn.replaceChildren(sign); btn.title = title; btn.setAttribute("aria-label", title); btn.dataset.state = state; };
    show(icon("copy"), t("web.markdown.copy_code"), "copy");
    let back, clicks = 0;
    btn.onclick = (e) => {
      e.stopPropagation();
      clearTimeout(back);
      const n = ++clicks;   // the result of an earlier click arriving later does not set its own reset
      // a failure gets its own icon: the title tooltip is not visible on a phone
      copyText(code.textContent.replace(/\n$/, "")).then(() => show(icon("allow"), t("web.markdown.copied"), "ok"),
        () => show(icon("deny"), t("web.markdown.copy_failed"), "fail"))
        .then(() => { if (n === clicks) back = setTimeout(() => show(icon("copy"), t("web.markdown.copy_code"), "copy"), 1500); });
    };
    pre.replaceWith(box);
    box.append(pre, btn);
  }
}

// a long AskUserQuestion with no line breaks, where the options are listed inline as "(1) …; (2) …" (an unreadable
// single-line numbering) — split it into an intro paragraph and a real markdown list, otherwise marked prints it all
// as one line of text, without indents
function numberedList(text) {
  const marks = [...text.matchAll(/\((\d+)\)/g)];
  const seq = [];
  for (const m of marks) {
    if (Number(m[1]) !== seq.length + 1) break;
    // the second and later items only right after ";": otherwise an "(N)" inside an ordinary sentence ("…of import (2)
    // and export (3) of data…", not an enumeration) is wrongly taken as the next item
    if (seq.length > 0 && !text.slice(0, m.index).trimEnd().endsWith(";")) break;
    seq.push(m);
  }
  if (seq.length < 2) return null;
  const first = seq[0].index;
  const after = marks[seq.length];   // a stray "(N)" later in the text (a reference to an option in an explanation) is not a list
  let end = after ? after.index : text.length;
  if (after) {
    const before = text.slice(first, end);
    const cut = Math.max(before.lastIndexOf(". "), before.lastIndexOf("; "));
    if (cut > 0) end = first + cut + 1;
  }
  const items = text.slice(first, end).split(/\(\d+\)\s*/).slice(1)
    .map((s) => s.replace(/[;.\s]+$/, "").trim()).filter(Boolean);
  if (items.length < 2) return null;
  const intro = text.slice(0, first).trim(), tail = text.slice(end).trim();
  return [intro, items.map((it, i) => `${i + 1}. ${it}`).join("\n"), tail].filter(Boolean).join("\n\n");
}

// splitting into paragraphs by sentence was dropped (it made things worse): instead, bold the essence itself — the
// last, question sentence ("Publish?", "Enable the check…?"), which is usually written at the end anyway; the emphasis
// leaves the rest of the text alone
function boldQuestion(text) {
  if (text.includes("**")) return text;   // it already has its own emphasis — leave it
  // with no leading sentence before the "?" there is nothing to emphasize — a short question ("Which option?") would
  // coincide with the "essence" entirely and turn fully bold, the very "everything bold" we moved away from
  const m = text.match(/[.!?]\s+([^.!?]*\?)\s*$/);
  if (!m) return text;
  const q = m[1].trim();
  const at = text.lastIndexOf(q);
  return text.slice(0, at) + `**${q}**` + text.slice(at + q.length);
}

// the words of a markdown text as the feed shows them, on one line: no ** or list dashes (a quote in the field).
// Parsed into an inert template: no images load and no diagram or copy button is made for it; a text with no words
// (an image alone, a rule) keeps its source. The last one is kept — the field is drawn on every snapshot
let plainLast = ["", ""];
export function plainText(text) {
  text = String(text || "");
  if (plainLast[0] === text) return plainLast[1];
  const flat = (s) => s.replace(/\s+/g, " ").trim();
  let words = "";
  if (ok) {
    const box = document.createElement("template");
    box.innerHTML = DOMPurify.sanitize(marked.parse(text, { breaks: true }), { ...CLEAN, FORBID_TAGS: FORBID });
    box.content.querySelectorAll("p, li, br, h1, h2, h3, h4, h5, h6, tr, td, th, pre, blockquote").forEach((n) => n.after(" "));
    words = flat(box.content.textContent);
  }
  plainLast = [text, words || flat(text)];
  return plainLast[1];
}

export function readable(text) {
  text = String(text || "").trim();
  if (!text.includes("\n") && text.length >= 160) {
    const list = numberedList(text);
    if (list) return boldQuestion(list);
  }
  return boldQuestion(text);
}

// breaks: a line break is a new line (that is how sessions write); files with hard width-wrapped layout use breaks: false
// media(src) — for documents in Files: the document's own images and videos (`![](shot.png)`) use the URL it
// returns; null — remove (foreign URLs: an image from a foreign site sees who opened it). Without media there are no
// images, as in the feed.
// the feed is rebuilt on every snapshot (during a turn — once a second), and every md() used to parse and sanitize
// the same message text again, and DOMPurify parses into a new document per call: a document per message per
// snapshot piled up between garbage collections. The sanitized markup depends only on the text and the flags — keep
// it; a hit moves to the end, so a long feed keeps all of its texts and only the least recently drawn one goes out.
// Bounded by size too, in characters of the text and of the markup (a string takes one or two bytes per character):
// Files converts a document of up to 2 MB again on every change on disk, and each version stayed here; a text above a
// quarter of the bound is not kept
const mdCache = new Map(), MD_CACHE = 5000, MD_CHARS = 8000000;   // more texts than a long loaded feed has: a pass over it in order must not push out its own start
let mdChars = 0;
const mdDrop = (key) => { mdChars -= key.length + mdCache.get(key).length; mdCache.delete(key); };
export function md(text, { breaks = true, media = null } = {}) {
  const b = el("div", "md");
  if (!ok) { b.style.whiteSpace = "pre-wrap"; b.textContent = text || ""; return b; }
  // media only picks the tag set: its rewrite happens on the copy below. The tables' wrappers and the highlighted code
  // are kept too (highlighting every code block of the feed again was the next cost of a snapshot); whether the
  // highlighter has loaded is a part of the key — the text is highlighted once it has
  const key = `${breaks ? 1 : 0}${media ? 1 : 0}${typeof hljs === "undefined" ? 0 : 1}${text || ""}`;
  let html = mdCache.get(key);
  if (html !== undefined) { mdDrop(key); b.innerHTML = html; }
  else {
    // documents in Files keep their own images and videos: media() rewrites each of their URLs below
    const own = ["img", "video", "audio", "source"];
    const forbid = media ? FORBID.filter((t) => !own.includes(t)) : FORBID;
    b.innerHTML = DOMPurify.sanitize(marked.parse(text || "", { breaks }), { ...CLEAN, FORBID_TAGS: forbid });
    textClasses(b);
    for (const t of b.querySelectorAll("table")) { const w = el("div", "tbl"); t.replaceWith(w); w.append(t); }
    highlightMd(b);   // a diagram's code has no highlighter language: it stays as it is for mermaidBlocks
    html = b.innerHTML;
  }
  const size = key.length + html.length;
  if (size <= MD_CHARS / 4) {
    while (mdCache.size && (mdCache.size >= MD_CACHE || mdChars + size > MD_CHARS)) mdDrop(mdCache.keys().next().value);
    mdCache.set(key, html);
    mdChars += size;
  }
  if (media) {
    for (const n of b.querySelectorAll("img[src], video[src], audio[src], source[src], video[poster]")) {
      for (const attr of ["src", "poster"]) {
        if (!n.hasAttribute(attr)) continue;
        const url = media(n.getAttribute(attr));
        if (url) n.setAttribute(attr, url); else if (n.tagName === "IMG") n.remove(); else n.removeAttribute(attr);
      }
    }
  }
  mermaidBlocks(b);
  copyButtons(b);
  return b;
}
