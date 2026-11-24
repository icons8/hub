// The session feed is a document, like a Claude Code transcript: › you, ● model, ● Tool(argument) ⎿ result.
import { fileNames, quotedBy } from "./quotefind.js";
import { $, el, icon, time, kb, elapsed, SPIN, REASONS, CHANNELS, stamp, kit, avatarNode, stepNode, chevron, OWN } from "./dom.js";
import { S, render, renderLive, foldOn, runningStep } from "./state.js";
import { banner, post } from "./api.js";
import { copyText, fileLink, md, linkAbsPaths, linkAll } from "./markdown.js";
import { diffEl, langOf, pre, readEl } from "./code.js";
import { byline, memberOf, nameOf, paint, whoOf, whoOfMember } from "./who.js";
import { t, CLOSED, TO, isolate } from "./i18n.js";
import "./lightbox.js";   // click on a feed image — enlarge it
import { chipRow } from "./mention.js";
import { castOf, castClock } from "./cast.js";
import { busRow, joinBus } from "./busthread.js";
import { picked } from "./cards.js";

// a session reply with file paths — links into "Files"; relative ones resolve from this session's folder; task numbers link to Jira
const cwdOf = (name) => (S.snap.sessions.find((s) => s.name === name) || {}).cwd;
// files the session itself opened and edited, newest first: `hub.json` in a reply refers to them
const knownOf = (name) => (S.snap.timelines[name] || []).filter((x) => x.kind === "tool" && x.input)
  .map((x) => x.input.file_path || x.input.notebook_path).filter((p) => typeof p === "string" && p.startsWith("/")).reverse();
const mdIn = (text, session, ver) => thumbs(linkAll(md(text), cwdOf(session), knownOf(session), session, S.snap.jira,
  S.snap.sessions.map((s) => s.name)), cwdOf(session), ver);  // paths both in `code` and in text

// images the message refers to (file:///…, paths, "Files" links) go under the text at a normal size; a click opens
// the lightbox. A relative path resolves from the session folder, ~ from its home folder
// paths that are the whole emphasised text (**/x.png:**) stay: cutting them left an empty emphasis ("- : first
// screens"); a path among other words of an emphasis is cut as in plain text. Judged by the outermost emphasis and
// everything cut out of it together (two paths in one, an emphasis inside another)
const EMPHASIS = "strong, b, em, i, del, s, mark, u";
function keepEmphasised(root, hide) {
  const outer = (h) => {
    let e = null;
    for (let p = h.bare ? h.bare.parentElement : h; p && p !== root; p = p.parentElement) if (p.matches(EMPHASIS)) e = p;
    return e;
  };
  const cut = new Map();   // emphasis → the texts unpath would take out of it
  for (const h of hide) {
    const e = outer(h);
    if (e) cut.set(e, [...(cut.get(e) || []), h.bare ? h.href : h.textContent]);
  }
  const left = (e, texts) => texts.reduce((s, x) => (x ? s.split(x).join("") : s), e.textContent);
  const keep = new Set([...cut].filter(([e, texts]) => !/[\p{L}\p{N}]/u.test(left(e, texts))).map(([e]) => e));
  return keep.size ? hide.filter((h) => !keep.has(outer(h))) : hide;
}
function thumbs(root, cwd, ver) {
  const seen = new Set(), guessed = new Set();
  // in reading order: links and bare file:///… in the text (they did not turn into links everywhere)
  const hrefs = [], walk = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  const where = new Map();   // address → where it is in the text: a path link or a bare file:///… (hide it when the image is shown below)
  while (walk.nextNode()) {
    const n = walk.currentNode;
    if (n.nodeType === 1) {
      if (n.matches("a[href], .path-wait")) {   // a path from the text is data-path (it may become a link later, markdown.js)
        const key = n.dataset.path ? "path:" + n.dataset.path : n.href;
        hrefs.push(key);
        // "screenshot" is a word, not a path; a path is judged by what was written (dataset.orig), the visible text may be a short name
        if ((n.dataset.orig || n.textContent).includes("/")) where.set(key, [...(where.get(key) || []), n]);
      }
      continue;
    }
    if (n.parentElement?.closest("a")) continue;
    for (const m of n.nodeValue.matchAll(/file:\/\/\/[^\s<>"'`)\]]+/g)) {
      const href = m[0].replace(/[.,:;!?]+$/, "");
      hrefs.push(href);
      where.set(href, [...(where.get(href) || []), { bare: n, href }]);   // not .text: for a link that is its text
    }
  }
  const hide = [];
  for (const href of hrefs) {
    let path = null;
    if (href.startsWith("path:")) path = href.slice(5);
    else try {
      const u = new URL(href, location.href);
      if (u.protocol === "file:") path = decodeURIComponent(u.pathname);
      else if (u.pathname === "/open" && u.searchParams.get("path")) path = u.searchParams.get("path");
    } catch { continue; }
    if (!path || !IMAGE.test(path)) continue;
    const bare = !path.startsWith("/");   // a name without a path is a guess about the session folder, not a fact
    const home = (cwd || "").match(/^\/(?:Users|home)\/[^/]+/)?.[0];
    if (path.startsWith("~/") && home) path = home + path.slice(1);
    else if (!path.startsWith("/") && cwd) path = `${cwd.replace(/\/$/, "")}/${path.replace(/^\.\//, "")}`;
    if (!path.startsWith("/")) continue;
    if (!bare) hide.push(...(where.get(href) || []));   // a guessed path is not hidden: the image may not exist
    if (seen.has(path)) continue;
    seen.add(path);
    if (bare) guessed.add(path);
  }
  if (!seen.size) return root;
  // the path to an image shown below is not needed in the text — in "Detailed" it stays as is
  if (S.compact && !S.brief.path) unpath(root, keepEmphasised(root, hide));
  const box = el("div", "thumbs" + (S.compact && !S.brief.big ? " mini" : ""));
  for (const path of seen) {
    // a guess already known to be missing — no preview and no three requests on every snapshot: its placeholder
    // flickered and pushed the text around (the chat text jumped); the file may appear — a background recheck, at most once per MISS_RECHECK
    if (guessed.has(path) && pictureFrom.get(path) === "none") { recheck(path); continue; }
    const img = pic("thumb:" + path, ver, () => {
      const img = el("img");
      img.alt = path.split("/").pop();
      img.title = t("web.feed.open_large", { name: img.alt });
      img.loading = "lazy";
      // failed to load — show the file name as a badge; a bare name with no file in the session folder (e.g. a
      // "screenshot.png" from another repo) is removed entirely: it was a path guess, not a sent image. The box is
      // wherever the node is now
      picture(img, path, ver, () => {
        if (!guessed.has(path)) return img.classList.add("broken");
        img.dataset.gone = "1";   // removed — if the file appears, the next re-render takes a new node (pic)
        const box = img.parentElement;
        img.remove();
        if (box && !box.children.length) box.remove();
      });
      return img;
    });
    box.append(img);
  }
  if (box.children.length) root.append(box);
  return root;
}

// the image comes from the page (/api/file: files the session showed to the owner; "Files" serves remote tailnet
// clients only the project folders); if that fails — from "Files", as before (on this machine it sees the whole disk)
// A second error after the src swap may be a late one from the first address (the image from "Files" is loading
// meanwhile) — "not found" only if the fallback address does not load in a probe either
// The page remembers where the image loaded from: the feed is rebuilt on every snapshot, and without this each
// re-render would ask /api/file again and flicker until the swap
const pictureFrom = new Map();
// Chrome keeps a picture by its address for the life of the tab, so a file rewritten under the same path showed its old
// look in the next message: the address carries the number of the message's event (as in a question card's preview). The
// number is part of the node's key too: one message keeps its node across re-renders, a later one loads anew
const verQuery = (ver) => (ver == null || ver === "" ? "" : "&v=" + encodeURIComponent(ver));
// an image that failed to load: the feed remembers that too, otherwise each re-render would show its placeholder (alt —
// a line of text) for the duration of three requests, and everything above it would jump
const missedAt = new Map(), MISS_RECHECK = 30000;
function missed(path) {
  pictureFrom.delete(path);
  pictureFrom.set(path, "none");
  missedAt.set(path, Date.now());
  if (pictureFrom.size > 500) pictureFrom.delete(pictureFrom.keys().next().value);
}
function recheck(path) {
  if (Date.now() - (missedAt.get(path) || 0) < MISS_RECHECK) return;
  missedAt.set(path, Date.now());
  const probe = new Image();
  probe.onload = () => { if (pictureFrom.get(path) === "none") pictureFrom.delete(path); };   // it appeared — pick it up from the next snapshot
  probe.src = `${FILES()}/raw?path=${encodeURIComponent(path)}`;
}
function picture(img, path, ver, failed = null) {
  const own = "/api/file?path=" + encodeURIComponent(path) + verQuery(ver), raw = `${FILES()}/raw?path=${encodeURIComponent(path)}${verQuery(ver)}`;
  img.addEventListener("load", () => {
    img.classList.remove("broken");
    pictureFrom.delete(path);
    pictureFrom.set(path, img.dataset.raw ? "raw" : "own");
    if (pictureFrom.size > 500) pictureFrom.delete(pictureFrom.keys().next().value);
  });
  img.addEventListener("error", () => {
    if (!img.dataset.raw) {
      img.dataset.raw = "1";
      img.src = raw;
    } else if (failed && !img.dataset.checked) {
      img.dataset.checked = "1";
      const probe = new Image();
      probe.onerror = () => { missed(path); failed(); };
      probe.src = raw;
    }
  });
  // already known missing — a badge at once and a single request, in case it has appeared
  if (failed && pictureFrom.get(path) === "none") {
    img.classList.add("broken");
    img.dataset.raw = img.dataset.checked = "1";
  }
  if (pictureFrom.get(path) === "raw") img.dataset.raw = "1";
  img.src = img.dataset.raw ? raw : own;
}

// a feed image is the same node between re-renders: the feed is rebuilt on every snapshot (during a turn — once a
// second), and /api/file serves no-store, so Safari reloaded a new <img> and showed it blank for a frame or two (the
// image flickered). The same image twice in one feed gets a second node: a node can sit in only one place.
// One that failed to load (a badge, a removed guess) gets a new node every time, as before: the file may have appeared
const pics = new Map(), picUses = new Map();   // key → nodes; key → how many were taken in this re-render
function pic(key, ver, make) {
  key += verQuery(ver);
  if (!picUses.size) queueMicrotask(() => picUses.clear());   // the feed is built synchronously — the re-render is over
  const n = picUses.get(key) || 0, list = pics.get(key) || [];
  picUses.set(key, n + 1);
  if (!list[n] || list[n].classList.contains("broken") || list[n].dataset.gone) list[n] = make();
  pics.delete(key);
  pics.set(key, list);
  if (pics.size > 300) pics.delete(pics.keys().next().value);
  return list[n];
}

// the player of a sent video is the same node between feed re-renders: a new node would interrupt playback and seeking
const players = new Map();   // event seq → <video>
function player(it) {
  const k = String(it.seq ?? it.path);
  let v = players.get(k);
  if (!v) {
    v = el("video", "sent-video");
    v.controls = true;
    v.preload = "metadata";
    v.playsInline = true;
    v.src = "/api/file?path=" + encodeURIComponent(it.path);
    v.title = it.caption || it.name;
    players.set(k, v);
    if (players.size > 50) players.delete(players.keys().next().value);
  }
  return v;
}

// a screen recording in the owner's words (js/cast.js): the player and under it "Screen recording · 0:05".
// The length comes from the apps' note; without one — from the video itself once its metadata is in
function castBlock(c, seq) {
  const box = el("div", "cast");
  const v = player({ seq, path: c.video.path, name: t("web.feed.screencast") });   // by the message: one file in two messages is two players
  const cap = el("div", "cast-caption");
  const say = (s) => { cap.textContent = Number.isFinite(s) && s > 0 ? t("web.feed.screencast_length", { time: castClock(s) }) : t("web.feed.screencast"); };
  say(c.seconds);
  if (!(c.seconds > 0)) {   // the apps write "0:00" when they do not know the length: take it from the video
    if (v.readyState >= 1) say(v.duration);
    else v.onloadedmetadata = () => say(v.duration);   // a property: a re-render replaces the handler, they do not pile up on the cached player
  }
  box.append(v, cap);
  return box;
}

// remove from the text the paths to the images shown: the link (with its `code`, if the code is only that) or a bare
// file:///…; parentheses left empty go too ("screenshot (/path)" → "screenshot", not "screenshot ()")
function unpath(root, hide) {
  const touched = new Set();
  for (const h of hide) {
    if (h.bare) {
      h.bare.nodeValue = h.bare.nodeValue.split(h.href).join("");
      touched.add(h.bare.parentNode);
      continue;
    }
    const code = h.closest("code");
    const node = code && code.textContent === h.textContent ? code : h;
    if (!node.parentNode) continue;   // already removed (two addresses of one link); the feed is not in the document yet — isConnected cannot be used
    touched.add(node.parentNode);
    node.remove();
  }
  for (const p of touched) {
    if (!p) continue;
    p.normalize();   // "screenshot (" and ") — check" become one text: empty parentheses are visible only this way
    for (const c of [...p.childNodes]) {
      if (c.nodeType === 3) c.nodeValue = c.nodeValue.replace(/\s*[(\[]\s*[)\]]/g, "").replace(/ {2,}/g, " ").replace(/ +([,.;:!?])/g, "$1");
    }
  }
}

// the "who · when · more" line: when is "12 min ago", the exact time on hover
function meta(head, ts, tail = "") {
  const m = el("div", "meta", head ? `${head} · ` : "");
  m.append(stamp(ts));
  if (tail) m.append(...after(tail));
  return m;
}
// " · status": a string, or a node that keeps itself up to date (a running step ticks)
const after = (tail) => [document.createTextNode(" · "), typeof tail === "string" ? document.createTextNode(tail) : tail];

// a line inserted mid-turn that the session has not read yet (the core: unread; Claude Code reads stdin only between
// steps) — said so, with the step it waits behind; the core sees the reading only as a turn with the message's mark
function unreadNote(m, name) {
  const n = el("span", "unread-note", m.label);
  n.title = t("web.feed.unread_hint");
  const step = runningStep(name, m.delivered_at);
  if (step) n.append(document.createTextNode(" · "), stepNode(step));
  return n;
}

// A Claude Code service notification (a subagent or a background command finished) reaches the session as "input" with
// <task-notification>… markup: these are not the owner's words. Take only the description and status from it — no ids, paths or result
const STATUS_WORDS = { failed: t("web.feed.status_failed"), error: t("web.feed.status_failed"), killed: t("web.feed.status_stopped"),
  stopped: t("web.feed.status_stopped") };
function serviceNote(it) {
  if (it.kind !== "terminal" || typeof it.text !== "string" || !/^\s*<task-notification/.test(it.text)) return "";
  const tag = (name) => (it.text.match(new RegExp(`<${name}>([\\s\\S]*?)</${name}>`)) || [])[1] || "";
  const summary = tag("summary").replace(/\s+/g, " ").trim();
  const bad = STATUS_WORDS[tag("status").trim().toLowerCase()];
  const status = (text) => (bad ? t("web.feed.with_status", { text, status: bad }) : text);
  let m = summary.match(/^Agent "(.*)" /);
  if (m) return status(t("web.feed.agent_done", { name: m[1] }));
  m = summary.match(/^Background command "(.*)" /);
  if (m) return status(t("web.feed.command_done", { name: m[1] }));
  return status(t("web.feed.service_message"));
}

function row(cls, glyph, body) {
  const r = el("div", "row " + cls);
  const g = el("span", "g", kit.avatars[glyph] ? "" : glyph);   // an emoji sign (🔔 ⛔ ⏰) — as the theme's image, if there is one
  if (kit.avatars[glyph]) g.append(avatarNode(glyph, "g-img"));
  g.setAttribute("aria-hidden", "true");
  const b = el("div", "b");
  body(b, g);
  r.append(g, b);
  return r;
}

// a long paste that went as a text file: the field names it pasted-<N>.txt (attachments.js), as the apps' Attachment.pasted;
// any other name, a session's paste-config.txt too, stays a file line with its name
const pastedFile = (f) => /^pasted-\d+\.txt$/.test(f.name || "") && !String(f.mime || "").match(/^(image|audio|video)\//);
// its first words, as the apps' PasteTile.title: whitespace folded, up to 80 characters (whole characters, not halves of an
// emoji); only a text answer becomes a title. A definite answer is kept, a refusal too (404 for a deleted file, 416 for an
// empty one, 401): the feed is rebuilt on every live event, and each draw would ask again. Only a network error or a 5xx
// (the hub restarting) is tried again, at most once per MISS_RECHECK, as a missed picture
const pasteTitles = new Map();   // href → the fetch in flight, the title ("" — keep the caption) or the time of a failed try
const pasteTitle = (s) => Array.from(Array.from(s).slice(0, 400).join("").split(/\s+/).filter(Boolean).join(" ")).slice(0, 80).join("");
function pasteHead(href) {
  const known = pasteTitles.get(href);
  if (typeof known === "number" && Date.now() - known < MISS_RECHECK) return Promise.resolve("");
  if (known !== undefined && typeof known !== "number") return Promise.resolve(known);
  const got = fetch(href, { headers: { Range: "bytes=0-4095" } }).then(async (r) => {
    if (r.status >= 500) return null;
    if (!r.ok || /^(image|audio|video)\//.test(r.headers.get("content-type") || "")) return "";
    const s = await r.text();
    return s.includes("\0") ? "" : pasteTitle(s);
  }).catch(() => null).then((s) => {
    pasteTitles.set(href, s === null ? Date.now() : s);
    return s || "";
  });
  pasteTitles.set(href, got);
  if (pasteTitles.size > 500) pasteTitles.delete(pasteTitles.keys().next().value);
  return got;
}
// the paste in the feed is a card of the apps' FeedTile: its first words and its size on a fill, a click gets the file;
// aria-busy while its words are read
function pasteTile(a, f) {
  a.classList.add("paste-tile");
  const head = el("span", "pt-title", t("web.feed.pasted_text")), size = el("span", "pt-size", kb(f.size || 0));
  const name = (words) => a.setAttribute("aria-label", `${t("web.feed.pasted_text")}: ${words ? words + ", " : ""}${kb(f.size || 0)}`);
  a.title = f.name || "";
  a.append(head, size);
  name("");
  const show = (s) => { a.removeAttribute("aria-busy"); if (s) { head.textContent = s; name(s); } };
  // a bubble (outgoing.js) has no event number yet: the row that takes its place reuses the title it read, not asks again
  const bare = a.href.replace(/&v=[^&]*$/, ""), key = pasteTitles.has(a.href) || !pasteTitles.has(bare) ? a.href : bare;
  const known = pasteTitles.get(key);
  if (typeof known === "string") return show(known);
  a.setAttribute("aria-busy", "true");
  pasteHead(key).then(show);
}

// a picture a bubble shows (outgoing.js, from the field's preview): the core's row of the same file takes this very node and
// points it at the file — the browser keeps the preview on screen until the file is in, so nothing blinks or jumps. A
// bubble's node no row took (refused, never shown) is let go a minute after the bubble was last drawn
const sendingPics = new Map(), PIC_KEEP = 60000;   // path → {img, at}
export function attachList(files, ver) {
  // the paste cards go first, in a row of their own; the other files (lines and pictures) start a line under them, as on a
  // phone: beside the cards a file line stuck to their top
  const box = el("div", "attach-list"), cards = el("div", "paste-row");
  for (const f of files || []) {
    const a = el("a");
    a.href = "/api/file?path=" + encodeURIComponent(f.path) + verQuery(ver);
    a.target = "_blank";
    a.rel = "noopener";
    if (pastedFile(f)) pasteTile(a, f);
    else if (String(f.mime || "").startsWith("image/")) {
      a.append(pic("att:" + f.path, ver, () => {
        const img = sendingPics.get(f.path)?.img || el("img");   // a bubble's own one, every time it is built
        sendingPics.delete(f.path);
        if (f.preview) {
          const now = Date.now();
          for (const [k, v] of sendingPics) if (now - v.at > PIC_KEEP) sendingPics.delete(k);
          sendingPics.set(f.path, { img, at: now });
        }
        if (!f.preview) img.src = a.href; else if (!img.src) img.src = f.preview;
        img.alt = f.name || t("web.feed.image");
        return img;
      }));
    } else {
      a.append(icon("attach"), document.createTextNode(`${f.name} · ${kb(f.size || 0)}`));
    }
    (a.classList.contains("paste-tile") ? cards : box).append(a);
  }
  if (cards.children.length) box.prepend(cards);
  return box;
}

function describeAsk(it) {
  if (it.tool === "AskUserQuestion") return (it.input.questions || []).map((q) => q.question).join("\n");
  return t("web.feed.allow", { tool: it.tool });
}

// ---- tool call: ● Name(argument)  ⎿ result; on click — the input, the edit diff and the full output ----
function toolArg(name, inp = {}) {
  const base = (p) => String(p || "").split("/").pop();
  if (name === "Bash") { const lines = String(inp.command || "").split("\n"); return lines[0] + (lines.length > 1 ? " …" : ""); }
  if (["Read", "Write", "Edit", "MultiEdit", "NotebookEdit"].includes(name)) return base(inp.file_path || inp.notebook_path);
  if (name === "Grep" || name === "Glob") return inp.pattern || "";
  if (name === "WebFetch") return inp.url || "";
  if (name === "WebSearch") return inp.query || "";
  if (name === "Task" || name === "Agent") return inp.description || "";
  return Object.values(inp).find((v) => typeof v === "string") || "";
}
// the "Files" module is on the same host, port 8788 (on a Mac — localhost, from a phone — the tailnet IP)
const FILES = () => `${location.protocol}//${location.hostname}:${S.snap?.files_port || 8788}`;
const IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i;
const toolName = (name) => { const m = String(name).match(/^mcp__(.+?)__(.+)$/); return m ? `${m[2]} (${m[1]})` : name; };

// input and output with syntax highlighting by file language; paths in the output are links
function toolDetails(it) {
  const full = el("div", "full");
  const inp = it.input || {}, file = inp.file_path || inp.notebook_path;
  if ((it.name === "Edit" || it.name === "MultiEdit") && it.input) full.append(diffEl(inp.edits || [inp], file));
  else if (it.name === "Write" && typeof inp.content === "string") full.append(pre(inp.content, langOf(file, inp.content)));
  else if (it.name === "Bash" && typeof inp.command === "string") full.append(pre(inp.command, "bash"));
  else full.append(pre(JSON.stringify(it.input, null, 2), "json"));
  if ((it.name === "Agent" || it.name === "Task") && it.id) full.append(subSteps(it));
  if (it.name === "Read" && IMAGE.test(file || "") && it.status !== "error") {
    const ver = it.seq ?? it.id;   // a sub-agent's step has no event number, but its tool call has its own id
    // the session read an image — show it (there is no text output for it, which used to leave an empty block)
    full.append(pic("tool:" + file, ver, () => {
      const img = el("img", "tool-img");
      picture(img, file, ver);
      img.alt = file;
      return img;
    }));
  } else if (it.result != null) {
    full.append(it.name === "Read" && it.status !== "error" ? readEl(it.result, file) : linkAbsPaths(el("pre", null, it.result), S.cur?.name));
  }
  full.addEventListener("click", (e) => e.stopPropagation());
  return full;
}

// Steps of a helper agent (otherwise edits an agent makes to code are not visible at all): Claude Code writes the
// agent's conversation to disk, and the server serves it by the Agent step id (/api/subagent). The feed is redrawn with
// every snapshot — what is loaded and expanded is kept here; while the agent works, steps keep loading.
const subCache = new Map();      // Agent step id -> {at, data, error}
const subOpen = new Set();       // which Agent steps show the agent's steps
const subStepOpen = new Set();   // expanded steps inside

async function loadSub(it) {
  subCache.set(it.id, { ...subCache.get(it.id), at: Date.now() });
  try {
    const r = await fetch(`/api/subagent?session=${encodeURIComponent(it.session || S.cur?.name || "")}&tool=${encodeURIComponent(it.id)}`);
    const j = await r.json();
    subCache.set(it.id, { at: Date.now(), data: r.ok ? j : null, error: r.ok ? null : t("web.feed.sub_missing") });
  } catch { subCache.set(it.id, { at: Date.now(), data: null, error: t("web.feed.sub_failed") }); }
  renderLive();   // loaded once per 3 s — like a server update: not in the middle of scrolling or a selection
}

function subSteps(it) {
  const box = el("div", "sub-steps"), shown = subOpen.has(it.id);
  const btn = el("button", "linkbtn", (shown ? "▾ " : CLOSED + " ") + t("web.feed.sub_steps"));
  btn.type = "button";
  btn.addEventListener("click", (e) => { e.stopPropagation(); shown ? subOpen.delete(it.id) : subOpen.add(it.id); render(); });
  box.append(btn);
  if (!shown) return box;
  const c = subCache.get(it.id);
  if (!c || (it.status === "running" && Date.now() - c.at > 3000)) loadSub(it);   // while it works — keep loading
  if (!c?.data) { box.append(el("div", "hint", c?.error || t("web.feed.loading"))); return box; }
  const list = el("div", "sub-list");
  for (const x of c.data.items) {
    if (x.kind === "said") { list.append(el("div", "sub-said", x.text)); continue; }
    const d = el("details", "sub-step");
    d.open = subStepOpen.has(x.id);
    const sm = el("summary"), arg = toolArg(x.name, x.input);
    const st = el("span", "st", x.status === "error" ? "✕" : "●");
    st.dataset.st = x.status;
    sm.append(st, el("b", null, toolName(x.name)), el("span", "arg", arg ? ` (${arg})` : ""));
    d.append(sm);
    const fill = () => { if (d.open && d.childElementCount === 1) d.append(toolDetails({ ...x, session: it.session || S.cur?.name })); };
    d.addEventListener("toggle", () => { d.open ? subStepOpen.add(x.id) : subStepOpen.delete(x.id); fill(); });
    fill();
    list.append(d);
  }
  if (!c.data.items.length) list.append(el("div", "hint", t("web.feed.sub_none")));
  box.append(list);
  return box;
}

// "Reply": the target is remembered together with the feed where it was pressed (view) — that feed's input field will show what is being replied to
// a reply takes the field off a question card (dock.js S.cardAim): the words go where the owner pointed last
export function replyTo(target) { S.replyTarget = { ...target, view: S.cur?.name }; S.cardAim = null; render(); $("msg")?.focus(); }
function replyBtn(label, target, cls = "reply-btn") {
  const btn = el("button", `linkbtn ${cls}`, label);
  btn.type = "button";
  btn.addEventListener("click", () => replyTo(target));
  btn.__reply = target;   // ⌘↑/⌘↓ pick a row to reply to by its button (js/msgkeys.js)
  return btn;
}
// "Copy" next to "Reply": the whole message; for a moment it says it copied (a theme shows its own sign), as the app's row does
function copyBtn(text) {
  const label = t("web.quote.copy"), btn = el("button", "linkbtn copy-btn", label);
  btn.type = "button";
  btn.title = label;
  let back = 0;
  btn.addEventListener("click", () => copyText(text).then(() => {
    btn.textContent = btn.title = t("web.markdown.copied");
    btn.classList.add("done");
    clearTimeout(back);
    back = setTimeout(() => { btn.textContent = btn.title = label; btn.classList.remove("done"); }, 1400);
  }, () => banner(t("web.markdown.copy_failed"))));
  return btn;
}

// emoji reactions (as in Telegram): on the owner's own message the core sets 👀 "taken" → 👍 "replied"; on a session's
// reply the owner answers with one reaction and can change it; clicking the same one removes it. The "Reactions"
// module (under "Modules", off by default): without it there are no badges and no "Reaction" button — the feed is as before
export const reactOn = () => (S.snap.features || []).includes("reactions");
export const REACTIONS = ["👍", "✅", "👀", "❤️", "🙏"];
const RECEIPTS = ["👀", "👍"];   // what the core puts under the owner's own message (REACT_SEEN, REACT_ANSWERED)
const badgeOf = (it) => {
  const b = ((S.snap.reactions || {}).badges || {})[it.kind === "in" ? it.id : String(it.seq)];
  // the "Core receipts" view checkbox: the snapshot does not tell the core's 👀/👍 from the owner's own, so both are hidden
  return !S.brief.receipts && it.kind === "in" && RECEIPTS.includes(b) ? undefined : b;
};
const flip0 = (key) => { S.openSteps.has(key) ? S.openSteps.delete(key) : S.openSteps.add(key); };
const flip = (key) => { flip0(key); render(); };
export function emojiBtn(cls, emoji, title) {
  const b = el("button", "linkbtn " + cls);
  b.type = "button";
  b.title = title;
  b.append(avatarNode(emoji, "react-emoji"));   // a theme with emoji images — its image
  return b;
}
// the owner's reaction; part — onto a selected fragment (js/quotesel.js): the core keeps it with the badge. true — accepted
export async function react(session, target, emoji, quote, part = false) {
  const r = await post("/api/react", { session, target: String(target), emoji, quote, ...(part ? { part: true } : {}) });
  if (r.error) banner(r.error);
  return !r.error;
}
// the core's 👍 under the owner's message leads to the row that answered it (reactions.links), in this feed only
function answerOf(it) {
  const link = it.kind === "in" && ((S.snap.reactions || {}).links || {})[it.id];
  return link && link.session === S.cur?.name && badgeOf(it) === "👍" && S.jumpAnswer ? link.seq : null;
}
// the targets whose 👍 already led to the answer: when the link arrives (👀 → 👍), a picker left open on the 👀 closes once —
// the chip is now the way to the answer, not the picker's switch ("Reaction" opens it again)
const linked = new Set();
function reactions(it, target, quote) {
  const cur = badgeOf(it), key = "r" + target, answer = answerOf(it);
  if (answer && !linked.has(key)) { linked.add(key); S.openSteps.delete(key); }
  const open = S.openSteps.has(key);
  // onto a selected fragment — only for a badge that is currently set (removed or displaced — the fragment is not its)
  const part = cur && it.kind !== "in" ? ((S.snap.reactions || {}).quotes || {})[String(target)] : null;
  const box = el("span", "reactions");
  const set = async (emoji) => {
    // changing a reaction on a fragment keeps it on the same fragment
    if (!await react(S.cur?.name, target, emoji, part || quote, !!part)) return;
    S.openSteps.delete(key);
    render();
  };
  if (cur) {
    const chip = emojiBtn("react-chip" + (answer ? " to-answer" : ""), cur, part ? t("web.feed.react_on", { quote: part })
      : answer ? t("web.feed.react_answer") : t("web.feed.react_change"));
    if (part || answer) chip.setAttribute("aria-label", chip.title);   // and for screen readers: what the reaction is on / where it leads
    if (!answer) chip.setAttribute("aria-expanded", String(open));
    // one press, one action: such a 👍 always goes to the answer; the reaction changes with "Reaction", as anywhere.
    // The link is read at the press: a row whose markup did not change keeps its node across snapshots (main.js), and
    // the link's row number is not in the markup
    chip.addEventListener("click", () => { const to = answerOf(it); return to ? S.jumpAnswer(it, to) : flip(key); });
    box.append(chip);
    // next to such a 👍 "Reaction" changes the reaction (the press on the 👍 is taken): here, beside the badge, so it never
    // wraps the time row on a phone. Classic only: Clean hides it (clean.css), there an own message carries the badge alone
    if (answer) box.append(reactBtn(target));
  }
  for (const e of open ? REACTIONS : []) {
    const b = emojiBtn("react-pick", e, e === cur ? t("web.feed.react_remove") : t("web.feed.react_pick", { emoji: e }));
    b.setAttribute("aria-pressed", String(e === cur));
    b.addEventListener("click", () => set(e === cur ? "" : e));
    box.append(b);
  }
  return box.childNodes.length ? box : null;
}
// "Reaction" on a session's reply sits next to "Reply" and behaves like it: visible on hover; a second click closes the picker
function reactBtn(target) {
  const add = el("button", "linkbtn react-add", t("web.feed.react_add"));
  add.type = "button";
  add.setAttribute("aria-expanded", String(S.openSteps.has("r" + target)));
  add.addEventListener("click", () => flip("r" + target));
  return add;
}

function toolRow(it) {
  const open = S.expanded.has(it.id);
  const r = row("tool", it.status === "running" ? SPIN[0] : "●", (b, g) => {
    g.dataset.st = it.status;
    if (it.status === "running") g.classList.add("spin");
    const call = el("div", "call");
    const arg = toolArg(it.name, it.input);
    const inp = it.input || {}, path = inp.file_path || inp.notebook_path;
    const argEl = el("span", "arg", arg ? `(${arg})` : "");
    if (typeof path === "string" && path.startsWith("/") && arg) {
      // the step's file is a link into "Files"
      const a = el("a", null, arg);
      a.href = `${FILES()}/open?path=${encodeURIComponent(path)}` + (S.cur ? `&session=${encodeURIComponent(S.cur.name)}` : "");
      a.target = "_blank";
      a.rel = "noopener";
      a.title = t("web.feed.open_in_files", { path });
      a.addEventListener("click", (e) => e.stopPropagation());
      argEl.replaceChildren("(", a, ")");
    }
    call.append(el("b", null, toolName(it.name)), argEl);
    call.title = arg;  // the line is cut to the width; in full on hover and on click
    b.append(call);
    // "Brief": a step is one line, the output on click; only the first line of an error is visible (less noise)
    const preview = S.compact && !S.brief.output ? (it.status === "error" ? 1 : 0) : 3;
    if (!open && preview && (it.result != null || it.status === "stopped")) {
      const lines = String(it.result || (it.status === "stopped" ? t("web.feed.stopped") : "")).split("\n");
      const res = el("div", "res");
      res.append(el("span", "elbow", "⎿"), el("span", "out", lines.slice(0, preview).join("\n") + (lines.length > preview ? "\n" + t("web.feed.more_lines", { count: lines.length - preview }) : "")));
      b.append(res);
    }
    if (open) b.append(toolDetails(it));
  });
  r.tabIndex = 0;
  r.setAttribute("role", "button");
  r.setAttribute("aria-expanded", String(open));
  r.setAttribute("aria-label", t("web.feed.tool_label", { name: toolName(it.name), action: open ? t("web.feed.collapse") : t("web.feed.show_io") }));
  const toggle = () => { open ? S.expanded.delete(it.id) : S.expanded.add(it.id); render(); };
  r.addEventListener("click", toggle);
  r.addEventListener("keydown", (e) => { if (e.target === r && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(); } });
  return r;
}

// a long message from another participant: whole while it fits two thirds of the feed's height, taller — cut to half that
// bar with "more". Decided once, on the page at first show (foldTall): a message shown whole stays whole when new ones
// come or the window gets smaller, an opened one stays open. An answered one (fold, the "Fold the answered" checkbox) is
// gray, two lines: so there is no guessing whether the owner has replied to it
const FOLD = 140;
const cuts = new Map();   // message key → the height it is cut to, px; 0 — shown whole
// a second "Reply" under a message taller than the feed's visible area: there is no wish to scroll back up having read it to
// the end. It is not drawn until the page has measured the message (foldTall, together with the fold): the text node is marked
// data-tall and carries what the button needs. Once the message was taller it keeps the button; one that was not is measured
// again at the next render
const talls = new Set();   // keys of the messages that were taller than the feed
function replyBottom(it, body, label, target, after) {
  const id = it.id ?? it.seq, key = "t" + it.kind + id;
  if (id == null) return null;
  if (talls.has(key)) return replyBtn(label, target, "reply-btn-bottom");
  body.dataset.tall = key;
  body.lowReply = { label, target, after };
  return null;
}
function clamped(it, body, fold = false) {
  const text = String(it.text || ""), lines = text.split("\n").length;
  // short — below the bar on any screen, not measured
  if (fold ? !(text.length > FOLD || lines > 2) : text.length < 500 && lines < 10) return body;
  const key = "m" + (it.id ?? it.seq), open = S.openSteps.has(key), cut = fold ? null : cuts.get(key);
  if (cut === 0) return body;
  const box = el("div", open ? "" : fold ? "clamp-2" : "clamp");
  box.append(body);
  const more = el("button", "linkbtn clamp-more", open ? t("web.feed.show_less") : t("web.feed.show_all"));
  more.type = "button";
  more.setAttribute("aria-expanded", String(open));
  more.addEventListener("click", () => { open ? S.openSteps.delete(key) : S.openSteps.add(key); render(); });
  const wrap = el("div");
  if (!fold && cut == null) { wrap.dataset.cut = key; more.hidden = true; }   // not decided yet: whole, foldTall measures it
  else if (!fold && !open) box.style.maxHeight = cut + "px";
  wrap.append(box, more);
  return wrap;
}

// where a tall message is cut, px from its top: after the last block that fits the budget, and the whole lines of the next
// paragraph when at least two fit — as in the app, the fold does not end on half a line or two and a half lines
function cutAt(box, budget) {
  const top = box.getBoundingClientRect().top, lineOf = (n) => parseFloat(getComputedStyle(n).lineHeight) || 24;
  let cut = 0;
  // a list goes item by item; a paragraph or an item is cut on whole lines; a block that does not fit ends the walk
  const walk = (blocks) => {
    for (const b of blocks) {
      const r = b.getBoundingClientRect();
      if (r.bottom - top <= budget) { cut = r.bottom - top; continue; }
      if (b.tagName === "UL" || b.tagName === "OL") return walk(b.children);
      const lines = b.tagName === "P" || b.tagName === "LI" ? Math.floor((budget - (r.top - top)) / lineOf(b)) : 0;
      if (lines >= 2) cut = r.top - top + lines * lineOf(b);
      return true;
    }
  };
  walk(box.firstElementChild?.children || []);
  // a short first paragraph before a long table or code: not one line and "more", the budget on whole lines
  if (cut < budget / 2) cut = Math.max(3, Math.floor(budget / lineOf(box))) * lineOf(box);
  return Math.ceil(cut);
}

// the feed is on the page: messages not decided yet are measured against two thirds of its height (not less than half the
// window, so an open keyboard does not cut everything short) — "more" only for very long messages, as in the app; the cut
// ends on a whole line. All measured first, then all written: one layout, not one per message
export function foldTall(feed) {
  if (!feed?.clientHeight) return;
  const bar = Math.max(feed.clientHeight, window.innerHeight / 2) * 2 / 3;
  const todo = [], tall = [];
  for (const body of feed.querySelectorAll(`[data-tall]${OWN}`)) {
    if (body.getClientRects().length) tall.push([body, body.getBoundingClientRect().height > feed.clientHeight]);
  }
  for (const wrap of feed.querySelectorAll(`[data-cut]${OWN}`)) {
    const box = wrap.firstElementChild;
    if (!box.getClientRects().length) continue;   // in a closed step: measured when shown
    todo.push([wrap, box, box.scrollHeight > bar ? cutAt(box, bar / 2) : 0]);
  }
  for (const [wrap, box, cut] of todo) {
    const key = wrap.dataset.cut;
    cuts.set(key, cut);
    delete wrap.dataset.cut;
    if (!cut) continue;
    if (!S.openSteps.has(key)) box.style.maxHeight = cut + "px";
    wrap.lastElementChild.hidden = false;
  }
  for (const [body, high] of tall) {
    if (!high) continue;   // not tall now: stays marked and is measured again at the next render (the feed may have changed its size)
    const { label, target, after } = body.lowReply;
    talls.add(body.dataset.tall);
    delete body.dataset.tall;
    after.after(replyBtn(label, target, "reply-btn-bottom"));
  }
}

// the words in a thread with another hub: rendered and folded like any message of another participant (the "Fold long
// messages between hubs" checkbox, on by default)
const hubWords = (text, id) => (S.brief.hubfold ? clamped({ id, text }, mdIn(text, S.cur?.name, id)) : mdIn(text, S.cur?.name, id));

// a quiet turn result "—" (a schedule check or `hub wait`, service): the turn has no reply — the "—" row is not drawn,
// and in "Brief" its in-turn remarks ("Looking…", "No changes") do not become a reply, they stay in the steps. Like Telegram
const silent = (it) => (it.kind === "out" || it.kind === "reply") && !it.error && String(it.text || "").trim() === "—";

// answered: a session reply before the turn that took the owner's next message (core, reactions.taken; commands,
// withdrawn and undelivered ones do not count); the last turn stays open. Only in "Brief" and with the "Fold answered" checkbox
function markResolved(list, compact) {
  const upto = foldOn() && compact && ((S.snap.reactions || {}).taken || {})[S.cur?.name] || 0;
  for (const it of list) it._resolved = it.kind === "out" && !it.error && it.seq < upto;
}

// "Brief": messages between sessions (hub, executors, schedule) — consecutive ones go into a single group row, collapsed
// in place; undelivered messages and tasks leave the group (they are not hidden). Returns a list where such entries are
// replaced by {kind: "group", seq, ts, items}
const quietMsg = (it) => it.kind === "in" && it.sender && !it.task && (S.snap.messages[it.id] || {}).state !== "failed"
  && !it.agent && !it.hubbus;

// a letter from an external agent (Codex, your own script) — the core sets the agent field on the row: the name of the
// agent a core module relayed the message from (core/views.py::_agent_of; the page does not parse a label in the text).
// The row is "Agent X · module Y", the time, "undelivered", "Reply", the quote it answers, and the gist in one line; the
// full text opens in place on a press on the gist, like a long message. The gist is the first line with letters, without
// markup and hidden ids; a long one is cut at the last sentence end within the first 90 characters (no shorter than 24)
// or at a word with "…"
export function letterGist(text, max = 90) {
  // a line is at most 1000 characters with single spaces before the replacements: on a long letter they stay linear
  const line = String(text || "").split(/\r?\n/).map((x) => x.slice(0, 1000).replace(/\s+/g, " ").replace(/^\s*(?:#{1,6}|[-*+>]|\d{1,3}[.)])\s+/, "")
    .replace(/[*`#]|(^|\s)_+|_+(?=\s|$)/g, "$1").replace(/[\s(]*\b[dmq]\d{4,7}\b\)?/g, "").replace(/\s+([;,.:])/g, "$1").replace(/\s+/g, " ").trim())
    .find((x) => /[\p{L}\p{N}]/u.test(x));
  if (!line) return "";
  if (line.length <= max) return line;
  const head = line.slice(0, max);
  for (let i = head.length - 2; i >= 24; i--) if (".!?;".includes(head[i]) && head[i + 1] === " ") return head.slice(0, i + 1).replace(/[;\s]+$/, "");
  const word = head.includes(" ") ? head.slice(0, head.lastIndexOf(" ")) : head;
  return word.replace(/[,:;—\-\s]+$/, "") + "…";
}
// the letter's byline: the agent's name and the module that relayed it; the letter is the agent's, the tint the module's
// (not that of a session with the same name)
function agentByline(it) {
  const b = el("span", "byline");
  const av = el("span", "av", Array.from(it.agent)[0].toUpperCase());   // the whole character: charAt splits an emoji in half
  av.setAttribute("aria-hidden", "true");
  b.append(av, el("b", null, t("web.who.agent", { name: it.agent, module: String(it.sender || "").replace(/^mod:/, "") })));
  return paint(b, whoOf(it.sender || "hub"));
}
// what is expanded is remembered while the page is open; the key is as for a long message (m<id>): the timeline search
// and following a quote expand the letter by themselves. Focus stays on the gist after the press
function letterRow(it, status) {
  const key = "m" + (it.id ?? it.seq), open = S.openSteps.has(key);
  const gist = letterGist(it.text);   // the text as the session got it; the page does not touch a label in it
  const r = row("me from letter", "", (b) => {
    const head = el("div", "meta");
    head.append(agentByline(it), document.createTextNode(" · "), stamp(it.ts));
    if (status) head.append(...after(status));
    if (S.cur) head.append(document.createTextNode(" · "), replyBtn(t("web.feed.reply"), { session: S.cur.name, quote: it.text }));
    b.append(head);
    if (it.quote) b.append(...inReply(it));
    // collapsed: the gist with ▸ (a press expands it); expanded: the text with "▾ collapse" under it, like a long message
    const btn = gist && el("button", "linkbtn letter-head");
    if (btn) {
      btn.type = "button";
      btn.setAttribute("aria-expanded", String(open));
      if (open) btn.append(chevron(true, "chat-chev"), el("span", null, t("web.feed.collapse")));
      else {
        btn.setAttribute("aria-label", `${t("web.feed.agent_letter", { name: it.agent })}: ${gist}`);
        btn.append(chevron(false, "chat-chev"), el("span", "chat-preview", gist));
      }
      btn.addEventListener("click", () => {
        S.openSteps.has(key) ? S.openSteps.delete(key) : S.openSteps.add(key);
        render();
        document.querySelector(`[data-key="${CSS.escape(keyOf(it))}"]${OWN} .letter-head`)?.focus();
      });
    }
    if (btn && !open) b.append(btn);
    if (open || !gist) b.append(mdIn(it.text, it.sender || "hub", it.seq));
    if ((it.attachments || []).length) b.append(attachList(it.attachments, it.seq));
    if (btn && open) b.append(btn);
  });
  paint(r, whoOf(it.sender || "hub"));
  return quotable(r, S.cur && { reply: { session: S.cur.name } });
}
export function groupRuns(list) {
  const out = [];
  for (const it of list) {
    const last = out[out.length - 1];
    if (!quietMsg(it)) { out.push(it); continue; }
    if (last && last.kind === "group") last.items.push(it);
    else out.push({ kind: "group", seq: it.seq, ts: it.ts, items: [it] });
  }
  return out;
}

// a schedule message in human text: without the service tag "[⏰ schedule <id>]" (the session reads it); for a
// `hub wait` check — "Check: waiting for a reply — <what>" without the instruction; the job and what is awaited come
// from the core (core/views.py), the text is not parsed. In "Detailed" — as is
function shownText(it) {
  // label — a short feed line (a button, a reply to a card), or the core's line as the owner reads it: "REPLIED dN: …"
  // without the tag and the numbers, in the hub's language (core/views.py row_label); text is raw, for "Send again".
  // The core's "REPLIED dN: …" (sender ":core", core/views.py CORE_SENDER) still has its tag in a row an earlier server
  // folded — an archive not parsed again yet, no label in an English hub: the tag is cut here, it is not for the owner
  const untagged = (x) => it.sender === ":core" ? x.replace(/^REPLIED\s+d\d+(?:\s*[(（][^)）]*[)）])?\s*[:：]\s*/, "") : x;
  if (it.label) return untagged(it.label);
  const text = untagged(String(it.text || ""));
  if (text !== String(it.text || "")) return text;
  if (!S.compact || S.brief.schedule || it.channel !== "schedule" || !it.job) return text;
  if (it.waiting) return t("web.feed.wait_check", { what: it.waiting });
  const mark = `[⏰ schedule ${it.job}]`;
  return text.startsWith(mark) ? text.slice(mark.length).trim() : text;
}

// a task message: the core says where the setter's words lie in the text (`brief`, [start, end) in string units); the
// rest is its frame for the model. null — the core did not mark it (an earlier version's task): the text as it is
function briefOf(it) {
  const text = String(it.text || ""), [a, b] = Array.isArray(it.brief) ? it.brief : [];
  return Number.isInteger(a) && Number.isInteger(b) && a >= 0 && a < b && b <= text.length ? text.slice(a, b) : null;
}

// the whole text a session got, folded under a button, like what the model got in a thread with another hub (js/busthread.js)
function svcFold(it) {
  // "An executor's whole task" in Brief: the frame is open until a click closes it
  const key = "s" + (it.id ?? it.seq), open = S.openSteps.has(key) !== (S.compact && S.brief.task);
  const svc = el("div", "hubth-svc");
  const btn = el("button", "linkbtn", `${open ? "▾" : CLOSED} ${t("web.feed.hub_svc")}`);
  btn.type = "button";
  btn.setAttribute("aria-expanded", String(open));
  btn.addEventListener("click", () => { flip0(key); render(); });
  svc.append(btn);
  if (open) svc.append(el("pre", null, String(it.text || "")));
  return svc;
}

function groupRow(g) {
  const key = "g" + g.seq;
  const open = S.brief.groups !== S.openSteps.has(key);   // with the "expanded" checkbox a click collapses, without it a click expands
  const r = row("chat-group", "", (b) => {
    const head = el("button", "chat-head");
    head.type = "button";
    head.setAttribute("aria-expanded", String(open));
    const names = el("span", "chat-names");
    for (const n of new Set(g.items.map((x) => x.sender))) names.append(byline(n));
    // strip markup, but keep hyphens and underscores inside words (otherwise "hub-dev-publish-2" showed as "hubdevpublish2")
    // from the last message — "what's new"; with the checkbox off — from the first, as before
    const first = shownText(g.items[S.brief.last ? g.items.length - 1 : 0]).replace(/^\s*(?:[-*+>]|#{1,6})\s+/gm, "").replace(/[*`#]|(^|\s)_+|_+(?=\s|$)/g, "$1")
      .replace(/\s+/g, " ").trim();
    const n = g.items.length;
    head.append(names, el("span", "chat-preview", first.slice(0, 90) + (first.length > 90 ? "…" : "")));
    // "N messages" can be turned off with a checkbox: the group can be expanded without it
    if (!S.compact || S.brief.count) head.append(el("span", "chat-count", t("web.feed.messages", { count: n })));
    head.append(chevron(open, "chat-chev"));
    head.addEventListener("click", () => { S.openSteps.has(key) ? S.openSteps.delete(key) : S.openSteps.add(key); render(); });
    b.append(head);
    if (open) { const body = el("div", "chat-body"); body.append(...g.items.map((x) => shownAs(KINDS.in(x), x))); b.append(body); }
  });
  return r;
}

// what can be done with a selected fragment of a row (js/quotesel.js): reply — the "Reply" target without a quote, react — the row for a reaction
const quotable = (r, q) => { if (q) r.__quote = q; return r; };
// who "Reply" goes to for a row: a session's reply — to it, another session's reply (reply) — to that one, with its delegation
const replyOf = (it) => (it.kind === "reply" ? { session: it.from, delegation: it.delegation } : { session: S.cur.name });
// who "Reply" goes to for a row that is not a session reply: a message from another session — to it, if it exists in
// the hub; the core, schedule, a module or a plugin are never sessions; the rest (notification, question, summary) —
// to this feed's session; a mini-hub executor's file — to it (by)
const known = (name) => !!name && name !== S.cur?.name && (S.snap.sessions || []).some((s) => s.name === name);
const quoteTo = (name) => ({ reply: { session: known(name) ? name : S.cur?.name }, react: null });

// jumping by a quote: a press leads to the row that was replied to — main.js (S.jumpQuote) finds it by the quote text.
// Here is what is visible: the highlighted row (flash until `until`), the step rows expanded for its sake (reveal —
// draw its step row even without the "Steps" checkbox, open — expand once), and what came of the quote (note: id → i18n key)
const ARRIVE_MS = 1200;   // calc(var(--dur) * 5) in feed.css
export const jump = { flash: null, until: 0, arrive: null, arriveAt: 0, reveal: new Set(), open: new Set(), note: new Map() };
export const keyOf = (it) => `${it.seq}${it.kind}${it.id || ""}`;   // feed row key (item: data-key)
const textsOf = new WeakMap();
// a row's text variants for matching a quote (js/quotefind.js): its raw text, what is on screen (a label), for an
// answer to a card — what was chosen, for a file — its caption and name; computed once per entry: the search walks the
// feed after every page
export function textsIn(it) {
  let v = textsOf.get(it);
  if (!v) textsOf.set(it, v = quotedBy(it, it.kind === "answer" ? answerWhat(it) : ""));
  return v;
}
// what the owner chose on a card: the answers, or "allowed"/"denied" for a permission
const answerWhat = (it) => (it.answers ? Object.values(it.answers).join(", ") : (it.behavior === "allow" ? t("web.feed.allowed") : t("web.feed.denied")));
const cards = new Map();   // request_id → { labels, multi } of a card with one question (the last CARDS_MAX cards seen)
const CARDS_MAX = 200;
function remember(ask) {
  const qs = ask.input?.questions;
  if (!Array.isArray(qs) || qs.length !== 1 || cards.has(ask.request_id)) return;
  const labels = (qs[0]?.options || []).map((o) => o?.label).filter(Boolean);
  if (!labels.length) return;
  cards.set(ask.request_id, { labels, multi: !!qs[0].multiSelect });
  if (cards.size > CARDS_MAX) cards.delete(cards.keys().next().value);
}
// the options of a one-question card the owner did not choose, as a colleague tells it: "chose X; not chosen: Y, Z".
// Unknown (nothing) when the page has not seen the card, it has several questions, or the answer is not made of its
// labels: own words typed in the card, or an answer with a comment after " — " this page did not send. The page's own
// pick (cards.js picked) counts only when the recorded answer is the one it sent: a refused tap, or a tap that lost
// to the one in flight, is not what the core recorded
function notChosen(it) {
  const card = cards.get(it.request_id);
  if (!card || !it.answers) return [];
  const got = String(Object.values(it.answers)[0] ?? "");
  const mine = picked.get(it.request_id);
  const same = mine && Object.values(mine.answers).length === 1 && String(Object.values(mine.answers)[0]) === got;
  const chosen = same ? Object.values(mine.marks)[0]
    : (got.includes(" — ") ? null : picks(got, [...card.labels].sort((a, b) => b.length - a.length)));
  if (!chosen || (!card.multi && chosen.length !== 1)) return [];   // "Yes, No" to a single pick: own words
  return card.labels.filter((l) => !chosen.includes(l));
}
// the labels an answer is made of (cards.js): picks joined with ", ", read as a whole from the start, longest label
// first, so "Yes, today" is one pick; null — the answer is not made of the labels. Each start is tried once (memo), so a
// contrived answer does not take exponential time
function picks(got, labels, i = 0, memo = new Map()) {
  if (memo.has(i)) return memo.get(i);
  let out = null;
  for (const l of labels) {
    if (!got.startsWith(l, i)) continue;
    const j = i + l.length;
    if (j === got.length) { out = [l]; break; }
    const more = got.startsWith(", ", j) ? picks(got, labels, j + 2, memo) : null;
    if (more) { out = [l, ...more]; break; }
  }
  memo.set(i, out);
  return out;
}
const NOTES = { seek: "web.feed.quote_seeking", none: "web.feed.quote_none", limit: "web.feed.quote_limit", fail: "web.feed.quote_failed" };
// the same for the way from the core's 👍 to the answer (main.js S.jumpAnswer: its mark is "answer:" + the message id, apart
// from the message's own quote); the key is dropped when the answer is found or the link is gone
const ANSWER_NOTES = { seek: "web.feed.answer_seeking", none: "web.feed.answer_none", limit: "web.feed.quote_limit", fail: "web.feed.quote_failed" };
function answerNote(it) {
  const key = "answer:" + it.id, note = jump.note.get(key);
  if (!note) return null;
  if (!answerOf(it)) { jump.note.delete(key); return null; }   // the link went away (the owner changed the badge): nothing to say
  return el("div", "hint quote-note", t(ANSWER_NOTES[note]));
}

// what the owner replied to: in "Brief" — one line "↩ "start…"", the whole quote as a tooltip; in "Detailed" and with
// the "quotes" checkbox — in full, like a session reply (otherwise it looks as if just a plain message was sent). A
// press goes to the source row; if it is not in the feed, the quote expands in full (a phone has no tooltips) with the reason next to it
function inReply(it) {
  const quote = it.quote, note = jump.note.get(it.id);
  const full = !S.compact || S.brief.quote;
  const line = full ? el("div", "quote quote-jump", t("web.feed.quote", { text: quote })) : el("div", "in-reply");
  if (!full) {
    line.append(icon("quote", "reply"), document.createTextNode(t("web.feed.quote", { text: quote.replace(/\s+/g, " ").trim() })));
    line.title = t("web.feed.in_reply", { text: quote });
    if (note && note !== "seek") line.classList.add("open");
  }
  line.setAttribute("role", "button");
  line.tabIndex = 0;
  const go = () => {
    if (!document.getSelection()?.isCollapsed) return;   // the quote text was being selected, not pressed
    if (S.jumpQuote?.(it) === false) line.classList.toggle("open");
  };   // without a feed — as before: expand
  line.addEventListener("click", go);
  line.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  return note ? [line, el("div", "hint quote-note", t(NOTES[note]))] : [line];
}


// ---- feed entries by kind ----
const KINDS = {
  in: (it) => {
    // a bubble (KINDS.outgoing) of known words has the head its row will have: delivered (the bubble stays while the row
    // is "typing"; the page's key carries the core's word in every catalog, tests/test_i18n.py); a voice one being
    // transcribed — the head of the page's row, no state
    const m = it.outgoing ? (it.outgoing === "words" ? { state: "delivered", label: t("web.feed.outgoing_delivered") } : {})
      : S.snap.messages[it.id] || {};
    const failed = m.state === "failed" ? t("web.feed.undelivered", { reason: (REASONS[m.reason] || m.reason) + (m.hook ? `: ${m.hook}` : "") }) : "";
    // the bus: words for this executor that its mini-hub got instead (core/views.py) — whom they went to, not its delivery state
    const status = it.relayed ? [t("web.feed.relayed", { name: nameOf(it.relayed) }), failed].filter(Boolean).join(" · ")
      : failed || (m.inserted_unread ? unreadNote(m, it.to || S.cur?.name)   // an "FYI" rider too: typed with its carrier
        : m.fyi ? t("web.feed.fyi") : (m.label || ""));   // hub send --fyi: arrived together with the next one, without a turn of its own
    if (it.agent && it.sender) return letterRow(it, status);
    if (it.hubbus) return busRow(it, hubWords);   // the words of another hub: a thread with it, not the core's frame
    const member = memberOf(it);   // a member of the Telegram group asks: a person's words, not the owner's
    const mine = !it.sender && !it.to && member === null;   // the owner's words: on the right, an accent plate; the rest — on the left with the author's name
    const quiet = S.compact && !S.brief.meta && mine && !it.relayed && !m.inserted_unread && (m.state === "delivered" || !m.state);
    const cast = mine && !it.label ? castOf(it) : null;   // a screen recording: the video and its length, not the note for the session
    const words = cast ? cast.text : shownText(it);   // what the owner sees as his words (a recording's own text without its note)
    const r = row(mine ? "me mine" : "me from", "", (b) => {
      if (mine) {
        b.append(quiet ? meta("", it.ts) : meta(t("web.feed.you_via", { channel: CHANNELS[it.channel] || it.channel }), it.ts, status));
        if (it.quote) b.append(...inReply(it));   // a reply made via "Reply": what it answers is visible next to the words
        if (it.tasks?.length) b.append(chipRow(it.tasks));   // the board cards he picked for this message (js/mention.js)
        if (cast) b.append(castBlock(cast, it.seq));
        // his words as a text block: a selected fragment of them gets the quote bar's "Reply" (js/quotesel.js)
        if (words) b.append(el("span", "own-text", words));
      } else {
        const head = el("div", "meta");
        head.append(member !== null ? byline(null, whoOfMember(member)) : byline(it.sender || "hub"), document.createTextNode(" · "), stamp(it.ts));
        if (it.to) head.append(document.createTextNode(` ${TO} ${isolate(nameOf(it.to))}`));
        if (status) head.append(...after(status));
        b.append(head);
        const lead = it.task ? briefOf(it) : null;
        if (lead != null) {
          // a task: the setter's words, the core's frame (the rules for the executor) folded as "for the model only"
          if (it.words != null) b.append(el("div", null, t("web.feed.quote", { text: it.words })));
          b.append(clamped({ ...it, text: lead }, mdIn(lead, it.sender || "hub", it.seq)), svcFold(it));
        } else if (S.compact && !S.brief.task && it.task) {
          // a task for an executor: in "Brief" — only the owner's words and the gist, without the task template
          b.append(document.createTextNode([it.words != null ? t("web.feed.quote", { text: it.words }) : "", it.context || ""].filter(Boolean).join("\n")));
        } else if (member !== null) {
          b.append(clamped(it, el("span", "own-text", shownText(it))));   // a person's words as they are, not markdown
        } else {
          // sessions, the hub and the core write markdown
          b.append(clamped(it, mdIn(shownText(it), it.sender || "hub", it.seq)));
        }
      }
      const files = cast ? cast.files : it.attachments || [];
      if (files.length) b.append(attachList(files, it.seq));
      // "Reply" on the owner's own words too: the quote is what is on screen, the reply goes to the session of this feed.
      // Files without words — the quote is their names, as on a file a session sent; "Copy" — only where there are words
      const said = words || fileNames(files);
      if (mine && S.cur && said) b.append(replyBtn(t("web.feed.reply"), { session: S.cur.name, quote: said }));
      if (mine && S.cur && words) b.append(copyBtn(words));
      if (mine && !it.relayed && !it.outgoing && reactOn()) {   // a reaction goes to the session that got the message
        // own message: 👀 → 👍 from the core, can be changed; a badge the owner removed (empty) is not brought back by the
        // core — "Reaction" sets it again. With no badge by the core's decision (failure, question) — no "Reaction":
        // on a phone it would sit on every old message. Next to a 👍 that leads to the answer it stands beside the badge
        // (reactions())
        if (badgeOf(it) === "") b.firstChild.append(reactBtn(it.id));   // in the time row: same height
        const rx = reactions(it, it.id, null);
        if (rx) b.append(rx);
        const note = answerNote(it);   // the way to the answer: "looking further back" / why it was not found
        if (note) b.append(note);
      }
    });
    if (!mine) paint(r, member !== null ? whoOfMember(member) : whoOf(it.sender || "hub"));
    return !S.cur ? r : quotable(r, mine ? quoteTo() : quoteTo(it.sender));
  },
  out: (it) => quotable(row("out" + (it.error ? " err" : "") + (it._resolved ? " resolved" : ""), "●", (b) => {
    const text = mdIn(it.text, S.cur?.name, it.seq);
    b.append(it._resolved ? clamped(it, text, true) : text);
    if (it.ts) b.append(meta("", it.ts));  // when it replied — under the reply
    // a reply to this message: the recipient will see in the label what was replied to
    if (!it.error && S.cur) {
      const target = { ...replyOf(it), quote: it.text };
      const top = replyBtn(t("web.feed.reply"), target);
      b.append(copyBtn(it.text), top);
      // a tall one, not folded to two lines — a second "Reply" under it, after the first in the block
      const low = it._resolved ? null : replyBottom(it, text, t("web.feed.reply"), target, top);
      if (low) top.after(low);
    }
    if (!it.error && S.cur && it.seq && reactOn()) {
      if (!badgeOf(it)) b.append(reactBtn(it.seq));
      const rx = reactions(it, it.seq, it.text);
      if (rx) b.append(rx);
    }
  }), !it.error && S.cur && { reply: replyOf(it), react: it.seq ? { session: S.cur.name, target: it.seq } : null }),
  bus: (it) => busRow(it, hubWords),
  tool: toolRow,
  group: groupRow,
  // hub send-file: a file for the owner — the image is visible right here, a click opens it in "Files"
  file: (it) => quotable(row("out sent-file", "●", (b) => {
    // by — a mini-hub executor (a file in the mini-hub's feed): caption paths and the link resolve from its folder, not the mini-hub's
    const who = it.by || S.cur?.name;
    b.append(meta(it.by ? t("web.feed.sent_you_by", { from: nameOf(it.by) }) : t("web.feed.sent_you"), it.ts));
    if (it.caption) b.append(mdIn(it.caption, who, it.seq));
    // "Reply" on a file too: the quote is its caption, or its name
    const reply = () => S.cur && b.append(replyBtn(t("web.feed.reply"), { ...quoteTo(it.by).reply, quote: it.caption || it.name }));
    // an image opens as the image itself (/raw), a document opens in the "Files" viewer: markdown is rendered there, not shown as raw text
    if (it.video) {   // a video plays in a player right here; the page serves exactly the sent file with Range
      b.append(player(it));
      reply();
      return;
    }
    // a document opens its source in "Files" (comments on it live there); if the source was deleted — its copy in the attachments (alt)
    const box = el("div", "attach-list"), a = it.image ? el("a") : fileLink(it.source || it.path, null, null, who);
    if (!it.image && it.source) a.href += "&alt=" + encodeURIComponent(it.path);
    if (it.image) {
      a.href = "/api/file?path=" + encodeURIComponent(it.path) + verQuery(it.seq);   // from the page: "Files" over the tailnet does not serve other folders
      a.target = "_blank";
      a.rel = "noopener";
    }
    if (it.image) {
      const img = pic("sent:" + it.path, it.seq, () => {
        const img = el("img");
        picture(img, it.path, it.seq);
        // open it large from wherever it loaded; the link is the one where the node is now
        img.addEventListener("load", () => { const a = img.closest("a"); if (a) a.href = img.currentSrc; });
        img.alt = it.caption || it.name;
        img.loading = "lazy";
        return img;
      });
      if (img.complete && img.currentSrc) a.href = img.currentSrc;
      a.append(img);
    } else a.append(icon("attach"), document.createTextNode(it.name));
    box.append(a);
    b.append(box);
    reply();
  }), S.cur && quoteTo(it.by)),
  notify: (it) => quotable(row("out notify" + (it.urgent ? " err" : ""), it.urgent ? "⛔" : "🔔", (b) =>
    b.append(meta(it.title, it.ts), md(it.text))), S.cur && quoteTo()),
  ask: (it) => quotable(row("out ask", "?", (b) => b.append(meta(t("web.feed.asks"), it.ts), it.tool === "AskUserQuestion"
    ? mdIn((it.input.questions || []).map((q) => q.question).join("\n\n"), S.cur?.name, it.seq)  // the question is markdown
    : el("div", null, describeAsk(it)))), S.cur && quoteTo()),
  // the owner's answer to a card: "Reply" quotes what he chose, like on his own message
  answer: (it) => quotable(row("sys answer", "↳", (b) => {
    const what = answerWhat(it), rest = notChosen(it), where = { channel: CHANNELS[it.channel] || it.channel, time: time(it.ts) };
    if (rest.length) b.append(el("span", "own-text", t("web.feed.answer_rest", { what, rest: rest.join(" · "), ...where })));
    else b.append(el("span", "own-text", t("web.feed.answer", { what, ...where })));
    if (S.cur) b.append(replyBtn(t("web.feed.reply"), { session: S.cur.name, quote: what }));
  }), S.cur && quoteTo()),
  terminal: (it) => {
    const note = serviceNote(it);
    if (note) return row("sys terminal", "·", (b) => { b.textContent = `${note} · ${time(it.ts)}`; });
    return row("me mine terminal", "⌨", (b) => b.append(meta(t("web.feed.terminal_input"), it.ts), document.createTextNode(it.text)));
  },
  // the head says who waits, the time is the stamp as on every row; the questions are the core's Markdown (a paragraph each)
  remind: (it) => row("remind", "⏰", (b) => b.append(
    meta(t("web.feed.remind_head", { session: it.session === S.cur?.name ? t("web.feed.this_session") : nameOf(it.session) }), it.ts),
    mdIn(it.md || it.text, it.session, it.seq))),
  reply: (it) => quotable(row("out", "●", (b) => {
    const box = el("div", "reply-box");
    // not the first result for a task — no quote of its message: a row of the same session, labeled with the task it belongs to
    const target = { ...replyOf(it), quote: it.text };
    const head = meta(it.again ? (it.task ? t("web.feed.replied_again", { from: nameOf(it.from), task: it.task }) : nameOf(it.from))
      : t("web.feed.replied", { from: nameOf(it.from) }), it.ts);
    head.append(document.createTextNode(" · "), copyBtn(it.text), replyBtn(t("web.feed.reply_to", { from: nameOf(it.from) }), target));
    box.append(head);
    if (it.quote && (!S.compact || S.brief.quote)) box.append(el("div", "quote", t("web.feed.quote", { text: it.quote })));
    const text = mdIn(it.text, it.from, it.seq);
    box.append(text);
    const low = replyBottom(it, text, t("web.feed.reply_to", { from: nameOf(it.from) }), target, text);
    if (low) box.append(low);
    b.append(box);
  }), { reply: replyOf(it) }),
  summary: (it) => quotable(row("out", "●", (b) => {
    const d = el("div", "summary");
    d.append(el("h3", null, t("web.feed.back", { from: time(it.from), to: time(it.to) })));
    const sec = (title, rows) => {
      d.append(el("div", "hint", title));
      if (!rows.length) { d.append(el("div", null, t("web.feed.nothing"))); return; }
      const ul = el("ul");
      for (const r of rows) {
        // a question asked in its absence is a card in its own session: a link to it while it is open
        const open = r.card && (S.snap?.sessions || []).some((s) => s.name === r.session && (s.pending || []).some((q) => q.id === r.card));
        if (!open) { ul.append(el("li", null, `${r.session}: ${r.text}`)); continue; }
        const a = el("a", null, t("web.strip.to_question")), li = el("li", null, `${r.session}: ${r.text} · `);
        a.href = "#/s/" + encodeURIComponent(r.session);
        li.append(a);
        ul.append(li);
      }
      d.append(ul);
    };
    // the hub's answers to cards for the owner (spec d2549241): first, and only when there are some (older summaries have none)
    if ((it.hub || []).length) sec(t("web.feed.hub_decided"), it.hub);
    // the deputies (spec d2679763): what they decided, and the cards at them with no answer — when there are some
    if ((it.deputies || []).length) sec(t("web.feed.deputies_decided"), it.deputies);
    if ((it.deputies_waiting || []).length) sec(t("web.feed.deputies_waiting"), it.deputies_waiting);
    sec(t("web.feed.waiting"), it.waiting || []);
    sec(t("web.feed.decided"), it.decided || []);
    sec(t("web.feed.done"), it.done || []);
    b.append(d);
  }), S.cur && quoteTo()),
};
// A page-side kind, not a journal record: the owner's message before the core has it (outgoing.js). It is drawn as the
// core's row of it will be (the same quote, cards, pictures in the same place and size, Copy/Reply line), so that row
// takes its place without a shift; only the head differs: "sending" in the place of its line (the core's row brings the time), or,
// for a voice message being transcribed, the head of that row and "Recognizing…" in the place of its words. Read by no
// one and not clickable: the core's row is the message (a screen reader would read it twice in the live feed)
KINDS.outgoing = (it) => {
  const r = KINDS.in({ kind: "in", outgoing: it.text != null ? "words" : "voice", ts: it.ts, channel: "web",
    text: it.text || "", quote: it.quote,
    tasks: it.tasks, attachments: it.files || [] });
  r.classList.add("outgoing");
  r.setAttribute("aria-hidden", "true");
  r.inert = true;
  const b = r.querySelector(":scope > .b");
  if (it.text != null) {
    // the word shows only if the core takes longer than a blink (feed.css); the delay counts from the send (a voice
    // message: not from the hold), not from this build — a row with a picture is built anew on every redraw (main.js
    // stamp), and the word must not blink
    // the head keeps the core's line, unseen (the same width and height as that row's), with the word over it
    const w = el("span", "outgoing-sending", t("web.feed.outgoing_sending"));
    const late = Math.max(0, 400 - Math.round(Date.now() - (it.left || Date.now())));
    if (late < 400) w.style.animationDelay = late + "ms";
    b.firstElementChild.classList.add("outgoing-head");
    b.firstElementChild.append(w);
  } else {
    const wait = el("span", "outgoing-wait");
    wait.append(el("span", "spin-dot"), el("span", null, t("web.voice.recognizing_dots")));
    b.append(wait);
  }
  return r;
};

// Several service rows in a row (restarts, "in the schedule…") form one dense block, not a string of dots
function detailed(list) {
  const out = [];
  let group = null, prev = null;
  for (const it of list) {
    if (silent(it)) continue;
    const sys = !KINDS[it.kind] && it.code !== "stopped";   // a stop is the owner's act: a row of its own
    if (sys && group) { group.append(el("div", null, `${it.text} · ${time(it.ts)}`)); continue; }
    const node = item(it);
    group = sys ? node.querySelector(".b") : null;
    if (it.kind === "in" && out.length) node.classList.add("turn");  // a new message is a new turn: top margin
    if (it.kind === "out" && prev !== "out") authored(node);
    prev = it.kind;
    out.push(node);
  }
  return out;
}

// the author's name at the start of a session's prose: whose remark it is is visible without the page header
function authored(node) {
  const b = node.querySelector(":scope > .b");
  if (b && S.cur) b.prepend(byline(S.cur.kind === "hub" ? "hub" : S.cur.name));
}

// "Brief": a turn = request → intermediate → reply. Requests, questions, replies and errors are visible; the
// intermediate part (calls, in-turn notes, service rows) is one line "▸ N steps", expanded in place.
const ANCHORS = new Set(["group", "in", "bus", "terminal", "ask", "answer", "reply", "summary", "remind", "notify", "file"]);
// a request comes from the owner, or is a task carrying the owner's words; messages from sessions, the hub, the core and
// the schedule are internal correspondence: they start a new turn but are shown as a caption in the steps row
// a member's question of the Telegram group has no sender either: it is a request (a person asked), Brief shows it
// an owner command to the core (/compact, /reload, buttons) and what became of it — the command mark from the core
// (core/views.py): "Brief" does not hide them — otherwise it looks as if the command did nothing — and they do not
// break a turn: a command in the middle of a turn does not make an intermediate remark the reply
const ownerAsk = (it) => it.kind !== "in" || !it.sender || it.task || (S.snap.messages[it.id] || {}).state === "failed"
  || !!it.agent || !!it.hubbus;   // undelivered ones are not hidden; an agent letter and a thread with another hub get their own row

function stepsLabel(mid, live) {
  const tools = {};
  let notes = 0, sys = 0;
  const from = mid.find((x) => x.kind === "in");
  for (const it of mid) {
    if (it.kind === "tool") { const n = toolName(it.name).split(" (")[0]; tools[n] = (tools[n] || 0) + 1; }
    else if (it.kind === "out") notes += 1;
    else if (it.kind !== "in") sys += 1;
  }
  const names = Object.entries(tools).sort((a, b) => b[1] - a[1]);
  const shown = names.slice(0, 3).map(([n, k]) => (k > 1 ? `${n} ×${k}` : n));
  const rest = names.slice(3).reduce((a, [, k]) => a + k, 0);
  const parts = [...shown, ...(rest ? [t("web.feed.rest", { count: rest })] : []), ...(notes ? [t("web.feed.notes", { count: notes })] : [])];
  const n = mid.filter((x) => x.kind === "tool" || x.kind === "out").length;
  // keys are sender names from the core (data), values are the "from whom" captions
  const FROM = { hub: t("web.feed.from_hub"), ":core": t("web.feed.from_core"), ":schedule": t("web.feed.from_schedule"),
    ":owner": t("web.feed.from_owner") };
  const said = from ? shownText(from).replace(/\s+/g, " ") : "";
  const who = from ? t("web.feed.from", { who: FROM[from.sender] || nameOf(from.sender), text: said.slice(0, 60) + (said.length > 60 ? "…" : "") }) : "";
  if (!n) return who || t("web.feed.service_events", { count: sys });
  const secs = Math.round(mid[n - 1].ts - mid[0].ts);
  const took = secs >= 5 ? ` · ${secs < 60 ? t("web.feed.took_s", { s: secs }) : t("web.feed.took_ms", { m: Math.floor(secs / 60), s: secs % 60 })}` : "";
  const last = [...mid].reverse().find((x) => x.kind === "tool");
  return (who ? who + " · " : "") + (live ? t("web.feed.working_steps", { count: n }) : t("web.feed.steps", { count: n }))
    + `: ${parts.join(", ")}` + (live && last ? " · " + t("web.feed.now", { tool: toolName(last.name).split(" (")[0] }) : took);
}

function stepsRow(mid, live) {
  const key = String(mid[0].seq);
  if (mid.some((x) => jump.open.delete(keyOf(x))) && S.brief.open === S.openSteps.has(key)) flip0(key);   // it was reached via a quote
  const open = S.brief.open !== S.openSteps.has(key);
  const wrap = el("div", "steps" + (open ? " open" : ""));
  const b = el("button", "steps-head");
  b.type = "button";
  b.setAttribute("aria-expanded", String(open));
  const g = el("span", "g" + (live ? " spin" : ""), live ? SPIN[0] : open ? "▾" : CLOSED);
  g.setAttribute("aria-hidden", "true");
  b.append(g, el("span", null, stepsLabel(mid, live)));
  b.addEventListener("click", () => { S.openSteps.has(key) ? S.openSteps.delete(key) : S.openSteps.add(key); render(); });
  wrap.append(b);
  if (open) { const inner = el("div", "steps-body"); inner.append(...detailed(mid)); wrap.append(inner); }
  return wrap;
}

// the retelling of the owner's answer to a card ("The owner's answer to question …", from the core to the session): the
// "↳" row of the same event sits next to it; in "Brief" with the "Hide the retelling…" checkbox it is not shown. The
// mark is the answer_to field plus a "↳" row with the same event number. It starts a turn — a turn boundary
// {kind: "cut"} takes its place; an undelivered one stays in view
function unrelay(list) {
  if (!S.brief.relay) return list;
  const answered = new Set(list.filter((x) => x.kind === "answer").map((x) => x.seq));
  return list.map((x) => (x.kind === "in" && x.answer_to && !x.task && answered.has(x.seq)
    && (S.snap.messages[x.id] || {}).state !== "failed" ? { kind: "cut", seq: x.seq, ts: x.ts } : x));
}

// a schedule turn answered "—" and showed nothing — its message is not shown (≈190 a day); with the "Hide checks…"
// checkbox off — as before. A turn is the entries after messages taken in a row, up to the next message; visible in
// it: a reply, an error, a question, an answer to a card, a file…, and with the "Steps" checkbox — the steps row; a
// running turn is not a result yet. A turn boundary {kind: "cut"} takes the message's place: neighboring turns do not
// merge. Schedule is the channel field from the core
const takes = (x) => (x.kind === "in" && !((x.command || x.relayed) && x.midturn)) || x.kind === "cut";
function unidle(list, running) {
  if (!S.brief.idle) return list;
  const drop = new Set();
  for (let i = 0; i < list.length;) {
    if (!takes(list[i])) { i++; continue; }
    let j = i, k;
    while (j < list.length && takes(list[j])) j++;
    for (k = j; k < list.length && !takes(list[k]); k++);
    const turn = list.slice(j, k);
    const outs = turn.filter((x) => x.kind === "out" && !x.error);
    const shown = turn.some((x) => (x.kind === "out" && x.error) || (x.kind !== "in" && ANCHORS.has(x.kind) && !silent(x))
      || (S.brief.steps && (x.kind === "tool" || (x.kind === "out" && !silent(x)))));
    if (outs.length && silent(outs[outs.length - 1]) && !shown && !(k === list.length && running)) {
      for (const x of list.slice(i, j)) if (x.channel === "schedule" && quietMsg(x)) drop.add(x);
    }
    i = k;
  }
  return drop.size ? list.map((x) => (drop.has(x) ? { kind: "cut", seq: x.seq, ts: x.ts } : x)) : list;
}

export function items(list, compact = S.compact, running = false) {
  list = joinBus(list);
  markResolved(list, compact);
  for (const x of list) if (x.kind === "ask" && x.request_id) remember(x);   // what an answer row chose from
  if (!compact) return detailed(list);
  const out = [];
  let mid = [];
  const chats = new Set();   // conversation groups in out — a line at the end, once the contents are known
  // which steps are drawn: without the "Steps of a finished turn" checkbox, only while the turn runs; a row reached via a
  // quote is among the steps: its steps row is drawn even without the checkbox
  const stepsOf = (part, live) => (part.length && (S.brief.steps ? part.some((x) => ["tool", "out", "in"].includes(x.kind)) || live
    : live || part.some((x) => jump.reveal.has(keyOf(x)))) ? stepsRow(part, live) : null);
  const close = (last) => {
    const live = last && running;
    const outs = mid.filter((x) => x.kind === "out" && !x.error);
    const hush = outs.length && silent(outs[outs.length - 1]);   // the turn ended with "—": no reply
    mid = mid.filter((x) => !silent(x));
    const errors = mid.filter((x) => x.kind === "out" && x.error);
    for (const e of errors) out.push(item(e));
    let rest = mid.filter((x) => !errors.includes(x));
    // every reply of the turn stays in view, each with the steps before it above it: a reply the owner has started
    // reading is not hidden in the steps when a later one ("saved to memory") comes, the running turn included
    if (!hush) {
      let from = 0;
      rest.forEach((x, i) => {
        if (x.kind !== "out") return;
        const node = item(x), steps = stepsOf(rest.slice(from, i), false);
        if (steps) { steps.classList.add("inline"); node.querySelector(".b").prepend(steps); }  // steps go above the reply
        if (!from || steps) authored(node);   // a reply right under the previous one: the name is already above
        out.push(node);
        from = i + 1;
      });
      rest = rest.slice(from);
    }
    const steps = stepsOf(rest, live);   // what came after the last reply: the running turn's steps, a reply-less turn
    if (steps) out.push(steps);
    mid = [];
  };
  for (const it of groupRuns(unidle(unrelay(list), running))) {
    if (it.kind === "reply" && silent(it)) continue;   // an executor's "—" under your message is not a reply
    if (it.kind === "cut") { close(false); continue; }   // a hidden message is a new turn, with no row
    if ((it.command && it.midturn) || it.relayed) {
      // the turn is running: its steps and reply come below, in one piece; words passed to the mini-hub start no turn
      // here — after a finished turn its reply is drawn above them (they are not a request it answered)
      if (!it.midturn) close(false);
      const node = item(it);
      if (!it.midturn && out.length) node.classList.add("turn");
      out.push(node);
    } else if (it.command) {
      close(false);
      const node = item(it);
      if (it.kind === "in" && out.length) node.classList.add("turn");
      out.push(node);
    } else if (ANCHORS.has(it.kind) && ownerAsk(it) && !serviceNote(it)) {
      close(false);
      const prev = out[out.length - 1];
      if (it.kind === "group") {
        // a conversation group is a line at the end, once its contents are known: when nothing is visible between two
        // groups (the turn answered "—", steps collapsed) it is one line, not one per message; a visible reply, error or
        // steps row lands in out between them and separates them
        if (chats.has(prev)) prev.items.push(...it.items);
        else { chats.add(it); out.push(it); }
        continue;
      }
      const node = item(it);
      if (it.kind === "in" && out.length) node.classList.add("turn");
      out.push(node);
    } else {
      if (it.kind === "in") close(false);  // someone else's message is a new turn: the previous reply stays in view
      mid.push(it);
    }
  }
  close(true);
  return out.map((x, i) => {
    if (!chats.has(x)) return x;
    const node = item(x);
    if (i) node.classList.add("turn");
    return node;
  });
}

// a text block is an entry row, except tool steps and service rows (an answer to a question counts as text); the
// "Brief" steps row carries no entry key. "Show earlier" uses it to search back to a text (main.js)
export const textBlock = (n) => !!n.dataset?.key && !n.matches(".row.tool, .row.sys:not(.answer)");

// which entry a feed row shows: hub.messageAt (ext.js, Plugin API 1.36) reads a row's message from it
const shown = new WeakMap();
const shownAs = (node, it) => { if (node?.nodeType === 1) shown.set(node, it); return node; };
// a feed row (or a node inside one) → the facts of its message for a plugin, or null (not a feed row). session — whose
// feed it is; kind — the feed kind ("in" a message to the session, "out" its reply...); mine — the owner's own words;
// target — what POST /api/react takes for this row (null: the row takes no reaction), as the feed's own reactions do;
// text — the message as it was written (markdown), null when the row has none
export function messageOf(node) {
  const r = node?.closest?.(".feed .row");
  const it = r && shown.get(r);
  if (!it || !S.cur || !KINDS[it.kind]) return null;   // a service line (a restart, "in the schedule…") is not a message
  const mine = it.kind === "in" && !it.sender && !it.to && !it.agent && !it.hubbus && memberOf(it) === null;
  // as the feed's own reactions: only while the "Reactions" module is on
  const target = !reactOn() ? null : it.kind === "out" ? (!it.error && it.seq ? String(it.seq) : null)
    : mine && !it.relayed && it.id ? String(it.id) : null;
  return Object.freeze({ session: S.cur.name, kind: it.kind, mine, target, text: typeof it.text === "string" ? it.text : null });
}

// a row kept on the page instead of its fresh build (main.js mergeFeed: the same markup) takes from the build what the
// markup does not show — the entry it stands for (hub.messageAt) and the target of a reply to a quote of it — for itself
// and the rows inside it (the same markup: the same rows in the same order)
export function adopt(old, fresh) {
  const was = [old, ...old.querySelectorAll(".row")], now = [fresh, ...fresh.querySelectorAll(".row")];
  now.forEach((n, i) => {
    const o = was[i];
    if (!o) return;
    if (shown.has(n)) shown.set(o, shown.get(n));
    if (n.__quote) o.__quote = n.__quote; else delete o.__quote;
  });
}

export function item(it) {
  const make = KINDS[it.kind];
  const node = make ? make(it) : row(it.code === "stopped" ? "sys stopped" : "sys", it.code === "stopped" ? "●" : "·", (b) => { b.textContent = `${it.text} · ${time(it.ts)}`; });
  if (node?.dataset) node.dataset.key = keyOf(it);   // the feed uses it to keep the reading position (main.js)
  if (node?.dataset && it.news) node.dataset.news = "1";   // a result that is new for the owner (the core): the "New" count and the mark go by it
  if (node?.classList && jump.flash === keyOf(it) && Date.now() < jump.until) node.classList.add("flash");
  if (node?.classList && jump.arrive === keyOf(it) && Date.now() - jump.arriveAt < ARRIVE_MS) {   // the "To the latest" button led here: the highlight goes on from where it was
    node.classList.add("arrive");
    node.style.animationDelay = `-${Date.now() - jump.arriveAt}ms`;
  }
  if (it.mod && node?.querySelector) modBadge(node, it.mod);
  return shownAs(node, it);
}

// a row from the entry of a module with the core.write right (its event or its edit of core handling) — a badge with the module name
function modBadge(node, name) {
  const b = node.querySelector(":scope > .b") || node;
  const badge = el("span", "mod-badge", `mod:${name}`);
  badge.title = t("web.feed.mod_badge", { name });
  b.prepend(badge);
}
