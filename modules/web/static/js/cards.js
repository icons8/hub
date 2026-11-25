// Cards above the input field: the session asks a question (AskUserQuestion), asks for permission, asks for a risky tool.
import { el, icon, RISKS } from "./dom.js";
import { S, render, sessionName } from "./state.js";
import { banner, newClientId, post, refusal } from "./api.js";
import { fileLink, linkAll, md, readable, whereIs } from "./markdown.js";
import { codeEl } from "./code.js";
import { t } from "./i18n.js";
import { fieldMic } from "./dictate.js";
import { fileFaceEl, openAskBox, reportFrame } from "./askbox.js";
import { addFiles, chipNodes, droppedFiles, pickFiles } from "./attachments.js";
import { boxes } from "./boxes.js";

function reasonInput() {
  const why = el("input");
  why.type = "text";
  why.placeholder = t("web.cards.reason_ph");
  why.setAttribute("aria-label", t("web.cards.reason_label"));
  return why;
}
function button(cls, iconName, label, type = "button") {
  const b = el("button", "btn " + cls);
  b.type = type;
  b.append(icon(iconName), document.createTextNode(label));
  return b;
}
function errorLine() { const e = el("p", "error-text"); e.setAttribute("role", "alert"); return e; }

// once answered, the card collapses at once without waiting for the core snapshot (seconds under load), and is not sent
// a second time: the answer used to arrive on the first click while the card hung there with an active button, and 7
// more clicks became 7 core refusals. Keys match the card cache's (dock.js ownerCards)
export const answered = new Set();
// request_id → what the card on this page sent: { answers, marks } — the labels picked for each question, null for the
// owner's own words (the feed tells what was not chosen only from picks it knows: an own answer "Yes — later" is not
// the pick "Yes"). Set only when the answer really went out, dropped when the core refused it, and used by the feed
// only when the recorded answer is this one (a tap refused with 409, then an answer from Telegram: not our pick)
export const picked = new Map();
const PICKED_MAX = 200;

// The answer goes out of a queue and does not wait for a receipt: the server replies once the core has recorded the
// answer (decide_wait, up to 3 s; under load it took 4–5 s from the click until the card went away), and a
// single-question card looked untouched all that time. On click it collapses into a line with the answer.
// Receipt: accepted — "sent" (dataset.got: its own answer arrived — a withdrawn question does not freeze the card, dock.js);
// queued (the core did not record it within decide_wait) — "queued", the outcome is polled with the same request and the
// same client_id (the server does not create a second fact, and a core refusal returns 409); no reply or a failure
// (api.js refusal) — retry; up to RETRY.tries times in total (~5 min). A refusal or running out of attempts (including
// the core never recording the answer) — the card expands with the reason (reopen): answer again or close; if frozen —
// the reason goes into a banner; if the user left the session page — it expands in the cache and the reason also goes
// into a banner.
// The outcome is awaited even after the question has left the snapshot: a late refusal is not lost. retry false — the
// endpoint has no client_id (/api/tools): a retry could duplicate the decision, so a failure is a refusal too
export const RETRY = { ms: [1000, 2000, 4000, 8000, 10000], tries: 33 };   // an object so tests can shorten the polling
function deliver(f, key, path, body, said, reopen, retry = true) {
  if (f.dataset.sending || f.dataset.frozen) return;
  f.dataset.sending = "1";
  answered.add(key);
  if (f.contains(document.activeElement)) document.activeElement.blur();
  f.classList.add("sent");
  const line = el("p", "sent-line");
  line.setAttribute("role", "status");
  f.append(line);
  const note = (k) => { line.textContent = t(k, { answer: said }); };
  note("web.cards.sending");
  const cid = newClientId();
  // dock.js reads these: whether the core recorded exactly this answer (the session's answered[] in the snapshot)
  if (retry) Object.assign(f.dataset, { cid, to: body.session, rid: body.request_id });
  let tries = 0;
  const again = (k) => { note(k); setTimeout(go, RETRY.ms[Math.min(tries++, RETRY.ms.length - 1)]); };
  const go = async () => {
    if (f.dataset.got) return;   // the core has already recorded it (snapshot) — nothing to ask
    const res = await post(path, retry ? { ...body, client_id: cid } : body);
    if (f.dataset.got) return;
    const queued = !res.error && res.data?.queued && retry;
    if (queued && tries < RETRY.tries) return again("web.cards.queued");
    if (!res.error && !queued) {
      f.dataset.got = "1";
      note("web.cards.sent");
      if (f.dataset.frozen) render();   // it froze while the answer was in flight: the answer arrived — the frozen card is not needed
      return;
    }
    if (!queued && retry && tries < RETRY.tries && !refusal(res.status)) return again("web.cards.sending_retry");
    const why = t("web.cards.not_sent", { error: queued ? t("web.cards.not_recorded") : res.error });
    const lost = f.__lost;   // a comment from the session's field (askCard __comment): its words go back there
    f.__lost = null;
    if (!answered.has(key) || f.dataset.frozen) { lost?.(why, false); return banner(why); }   // no card — the reason goes into a banner
    // the session page was switched in the meantime (the node is not on screen) — the card stays collapsed in the cache
    // while the question is open: we undo the collapse and return it expanded, and also show the reason in a banner
    answered.delete(key);
    delete f.dataset.sending;
    f.classList.remove("sent");
    line.remove();
    reopen(why);
    lost?.(why, true);   // after the card is open again (in the cache while off screen): the field can aim at it again
    if (!f.isConnected) banner(why);
  };
  go();
}

export function toolCard(s, r) {
  const f = el("form", "card");
  const info = S.snap.catalog[r.tool] || {};
  const risk = (info.risk || []).length ? t("web.cards.risk", { list: info.risk.map((x) => RISKS[x] || x).join(", ") }) : "";
  // what of the plugin runs code (hooks, servers, programs): the owner decides on exactly that
  const runs = (info.runs || []).length ? t("web.cards.runs", { list: [...info.runs, ...(info.runs_more ? [t("web.cards.runs_more", { count: info.runs_more })] : [])].join("; ") }) : "";
  // the header has the same style as the question window's
  const head = el("div", "card-head");
  head.append(el("h3", null, t("web.cards.connect_title", { tool: r.tool })));
  f.append(head, el("p", "hint", [info.description, r.why ? t("web.cards.why", { why: r.why }) : "", risk, runs].filter(Boolean).join(" · ")));
  const why = reasonInput(), err = errorLine(), btns = el("div", "btns");
  const yes = button("primary", "allow", t("web.cards.connect")), no = button("", "deny", t("web.cards.decline"));
  // the question was withdrawn — the card is frozen (dock.js freeze), deliver sends nothing from it
  const act = (action, b) => deliver(f, `${s.name}:tool:${r.tool}`, "/api/tools",
    { session: s.name, tool: r.tool, action, reason: why.value }, b.textContent, (e) => { err.textContent = e; }, false);
  yes.addEventListener("click", () => act("approve", yes));
  no.addEventListener("click", () => act("deny", no));
  btns.append(yes, no);
  f.append(why, err, btns);
  return f;
}

// an executor's spec waits for the owner (core/specmode.py): open it in "Files", accept it or drop it. "Accept" takes the
// file as it is at the press
export function specCard(s, x) {
  const f = el("form", "card spec-card");
  const head = el("div", "card-head");
  head.append(el("h3", null, t("web.cards.spec_head", { id: x.spec.task, name: sessionName(x.name) })));
  const hint = el("p", "hint", t("web.cards.spec_hint"));
  const err = errorLine(), btns = el("div", "btns");
  const open = el("a", "btn");
  open.textContent = t("web.cards.spec_open");
  open.hidden = true;
  const yes = button("primary", "allow", t("web.cards.spec_accept")), no = button("", "deny", t("web.cards.spec_drop"));
  const peek = async () => {
    const r = await post("/api/spec", { delegation: x.spec.task, action: "peek" });
    if (r.error) { err.textContent = r.error; return; }
    const link = fileLink(r.data.path, "", null, x.name);
    open.href = link.href;
    open.target = "_blank";
    open.rel = "noopener";
    open.hidden = false;
  };
  const act = async (action, b) => {
    err.textContent = "";
    b.disabled = true;
    const r = await post("/api/spec", { delegation: x.spec.task, action });
    b.disabled = false;
    if (r.error) { err.textContent = r.error; return; }
    f.classList.add("sent");
    f.replaceChildren(el("p", "hint", t(action === "accept" ? "web.cards.spec_accepted" : "web.cards.spec_dropped",
      { id: x.spec.task })));
  };
  yes.addEventListener("click", () => act("accept", yes));
  no.addEventListener("click", () => act("drop", no));
  btns.append(open, yes, no);
  f.append(head, hint, err, btns);
  peek();
  return f;
}

// the question's markdown is parsed the same way as an ordinary session message (markdown.js:linkAll, the same pipeline
// as feed.js:mdIn): files, Jira issues, sessions — a style no different from an ordinary dialogue
function rich(text, s) {
  return linkAll(md(text), s.cwd, [], s.name, S.snap.jira, S.snap.sessions.map((x) => x.name));
}

// close a question without answering (Esc, ×): the core withdraws it everywhere and tells the session "no answer will come"
export async function dismissAsk(f) {
  if (f.dataset.closing || f.dataset.frozen || f.dataset.sending) return;   // the answer is already on its way — nothing to close
  f.dataset.closing = "1";
  const res = await post("/api/question/dismiss", { session: f.dataset.asker || f.dataset.session, request_id: f.dataset.ask });
  if (res.error) { delete f.dataset.closing; f.querySelector(".error-text").textContent = res.error; return; }
  f.__done?.();   // closed: the session's field is no longer aimed at it (dock.js)
  f.hidden = true;   // the core snapshot will remove the card entirely
}

// An option's `preview` (AskUserQuestion; a variant is judged by sight): an ASCII sketch — a
// monospace block, as written — or one path to a picture (/abs, ~/, file://): it is shown from the page (/api/file serves
// what the session showed; if not — from "Files", as the feed's images do); on the option's row it is a thumbnail, a click opens it large (askbox.js).
// One path to anything else (a report.html, a pdf, a folder) is a file, never the bare path in monospace: an html report
// shows itself, live and small; a click opens it whole in a new tab (fileThumb).
// The same rule as core/shown.py preview_image
const PICTURE = /^(?:file:\/\/)?(?:~|\/|[A-Za-z]:[\\/])\S[^\n]*\.(?:png|jpe?g|gif|webp|svg)$/i;
// one line that starts like an absolute path, with a name character right after the root: "/ \" in a sketch is not one
const FILE_PATH = /^(?:file:\/\/)?(?:~\/|\/|[A-Za-z]:[\\/])[\w.@~][^\n]*$/;
const PREVIEW_MAX = 20000;
const FILES = () => `${location.protocol}//${location.hostname}:${S.snap?.files_port || 8788}`;
function pathAt(text, cwd, re) {
  const one = text.trim();
  if (one.length > 1024 || !re.test(one)) return null;
  let path = one;
  if (path.startsWith("file://")) {
    try { path = decodeURIComponent(path.slice(7)).replace(/^\/(?=[A-Za-z]:)/, ""); } catch { return null; }
  }
  const home = (cwd || "").match(/^\/(?:Users|home)\/[^/]+/)?.[0];
  return path.startsWith("~/") && home ? home + path.slice(1) : path;
}
// a file preview: the icon and the name, for the thumbnail and the big view; `dir` — a folder or a name without an extension
function fileFace(path) {
  const name = path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
  const dir = /[\\/]$/.test(path) || !/\.[^.]+$/.test(name);
  return { name, dir };
}
// An option's preview as a small button on the option's row (a phone shows several options at once): a click opens it large in
// askbox.js with paging through the card's options and "Choose this". `item` is what the big view draws: a picture
// (src follows the thumbnail: /api/file, then "Files"), an ASCII sketch, a file, or a line when the picture is gone.
// `ver` (the question's request id) goes into the address: the browser keeps a loaded picture per address for as long as the
// page lives (even under no-store), so a file rewritten under the same path would show its old look in the next question.
// the corner mark of a thumbnail: "expand" opens the big view, "open" a new tab (an icon of the set, not a character a
// font may lack)
function thumbMark(name) {
  const m = el("span", "opt-thumb-mark");
  m.append(icon(name));
  return m;
}
function previewThumb(text, label, cwd, ver) {
  const path = pathAt(text, cwd, PICTURE);
  if (!path) {
    const file = pathAt(text, cwd, FILE_PATH);
    if (file) return fileThumb(file, label, cwd);
  }
  const thumb = el("button", "opt-thumb"), item = { label };
  thumb.type = "button";
  thumb.title = thumb.ariaLabel = t("web.cards.preview_open", { label });
  if (!path) {
    const pre = el("pre", "opt-thumb-pre");
    item.kind = "text";
    item.text = text.replace(/^(?:[ \t]*\n)+/, "").trimEnd().slice(0, PREVIEW_MAX);
    pre.textContent = item.text.split("\n").slice(0, 10).join("\n");
    pre.setAttribute("aria-hidden", "true");
    thumb.append(pre, thumbMark("expand"));
    return { thumb, item };
  }
  const img = el("img", "opt-thumb-img");
  img.alt = "";
  item.kind = "img";
  const stamp = ver ? "&v=" + encodeURIComponent(ver) : "";
  item.src = "/api/file?path=" + encodeURIComponent(path) + stamp;
  img.addEventListener("error", () => {
    if (!img.dataset.raw) {
      img.dataset.raw = "1";
      item.src = `${FILES()}/raw?path=${encodeURIComponent(path)}${stamp}`;
      img.src = item.src;
    } else {
      item.kind = "missing";
      item.text = t("web.cards.preview_missing", { path });
      thumb.classList.add("missing");
      thumb.replaceChildren(el("span", "opt-thumb-miss", t("web.cards.preview_gone")));
    }
  });
  img.src = item.src;
  thumb.append(img, thumbMark("expand"));
  return { thumb, item };
}
// a path to a file that is not a picture. An html report is its own live frame, small and inert, from "Files" /render —
// the address "Files" already shows reports at, its css and frames/*.png beside it load by their relative paths (a frame
// of the file:// address would show nothing, and no new way of serving files is needed); a pdf, a folder or another file
// is its icon and name. A click opens it in a new tab: the report whole (/render), a pdf in the browser's viewer (/raw),
// a folder or anything else in "Files". Where the path leads asks the server, as a path in the feed does (whereIs): a file
// it does not find stays a name without a link
const RENDER_W = 1280;   // the width an html report is laid out at in its thumbnail, then scaled down to the thumbnail
function fileThumb(path, label, cwd) {
  const { name, dir } = fileFace(path);
  const html = !dir && /\.html?$/i.test(name), pdf = !dir && /\.pdf$/i.test(name);
  const thumb = el("div", "opt-thumb opt-file"), a = el("a");
  a.target = "_blank";
  a.rel = "noopener";
  a.title = a.ariaLabel = t("web.cards.file_open", { name });
  a.append(fileFaceEl(name, dir));
  thumb.append(a);
  const item = { label, kind: "file", name, dir };
  item.where = whereIs(path, cwd).then((got) => {
    if (!got.open && !got.file) { thumb.classList.add("missing"); a.removeAttribute("title"); a.removeAttribute("aria-label"); return {}; }
    const files = FILES(), render = html && got.open ? `${files}/render/${got.open.slice(2)}` : null;
    // by the root and the inner path the server resolved ("~/x.pdf" is not an absolute path to /raw); a file only the page
    // itself serves (a file the session sent, outside the project folders over the tailnet) is its own address
    const [root, ...inner] = got.open ? got.open.slice(2).split("/") : [];
    a.href = got.open ? render || (pdf ? `${files}/raw?root=${root}&path=${inner.join("/")}` : `${files}/${got.open}`) : got.file;
    if (render) {
      const frame = reportFrame(render);
      frame.classList.add("opt-file-frame");
      a.replaceChildren(frame, thumbMark("open"));
      thumb.classList.add("live");
      // the report is laid out wide and scaled to the thumbnail, whatever its size (112×72 on a wide screen, the full
      // row on a phone)
      new ResizeObserver(() => {
        const k = thumb.clientWidth / RENDER_W;
        if (k > 0) { frame.style.height = `${Math.ceil(thumb.clientHeight / k)}px`; frame.style.transform = `scale(${k})`; }
      }).observe(thumb);
    }
    return { href: a.href, render };
  });
  return { thumb, item, opens: true };
}
// the paths of attached files go into the answer's text, under the words (the model reads files by path; the session gets
// them as it gets the chat field's: an upload under attachments/). The first line stays what the owner chose
const FILES_HEAD = "Attached files:";
const withFiles = (text, files) => (files.length ? (text ? text + "\n\n" : "") + FILES_HEAD + "\n" + files.map((p) => `- ${p.meta.path}${p.label ? ` ([${p.label}])` : ""}`).join("\n") : text);

const MORE_CONTEXT = "Need more context";   // core/owner.py MORE_CONTEXT: an answer a client sent in English; the page sends its label

export function askCard(s, req) {
  const f = el("form", "card ask-card");
  f.dataset.session = s.name;
  f.dataset.ask = req.request_id;
  // a session's question shown by the hub in its own chat (hub question show): the same card as in that session's chat, the answer goes to it
  const asker = req.session || s.name;
  f.dataset.asker = asker;
  const head = el("div", "card-head"), close = el("button", "card-x");
  close.type = "button";
  close.title = t("web.cards.close_title");
  close.setAttribute("aria-label", t("web.cards.close_label"));
  close.append(icon("close"));
  close.addEventListener("click", () => dismissAsk(f));
  // "reply": aims the session's field at this card (dock.js sets __aim) — the words from the field go as its comment
  const reply = el("button", "card-aim");
  reply.type = "button";
  reply.title = t("web.cards.aim_title");
  reply.setAttribute("aria-label", t("web.cards.aim_title"));
  reply.setAttribute("aria-pressed", "false");
  reply.append(icon("reply"));
  reply.addEventListener("click", () => f.__aim?.());
  head.append(el("h3", null, asker === s.name ? t("web.cards.ask_head") : t("web.cards.ask_head_of", { name: sessionName(asker) })), reply, close);
  f.append(head);
  const qs = req.input.questions || [];
  // a single question with a single choice — tapping an option is the answer, with no radio circle and no "Answer"
  // button; several questions, or one question with multiple choices — pick and then "Answer" (otherwise the last tap
  // used to submit the whole card and there was no way to change one's mind about an upper question)
  // a card with previews is looked at first: a tap only chooses, "Choose" answers
  const looked = qs.some((q) => (q.options || []).some((o) => typeof o.preview === "string" && o.preview.trim()));
  const instant = qs.length === 1 && !qs[0].multiSelect && !looked;
  if (instant) f.classList.add("instant");
  const keys = [];   // draft key of each question's attachments
  qs.forEach((q, i) => {
    // the separator between questions is a separate <hr>, not the fieldset border: that one breaks on <legend> (the
    // browser cuts border-top at the legend, so the line comes out as a stub)
    if (i > 0) f.append(el("hr", "q-sep"));
    const fs = el("fieldset");
    // the question and the option descriptions are markdown, like session replies (otherwise questions rendered as plain text)
    const legend = el("legend");
    // several questions in one card visually ran together: a number above each, except a lone one; inside legend — the
    // browser hoists <legend> to the top regardless of the fieldset children's order, so a label outside it ended up in the wrong place
    if (qs.length > 1) legend.append(el("div", "q-of", t("web.cards.q_of", { n: i + 1, total: qs.length })));
    legend.append(rich(readable(q.question), s));
    fs.append(legend);
    const shown = [];   // the options that have a preview, for the big view
    for (const o of q.options || []) {
      const lab = el("label", instant ? "opt pick" : "opt"), r = el("input"), txt = el("span");
      r.type = q.multiSelect ? "checkbox" : "radio";
      r.name = "q" + i;
      r.value = o.label;
      txt.append(document.createTextNode(o.label));
      if (o.description) { const sm = el("small"); sm.append(rich(o.description, s)); txt.append(sm); }
      if (typeof o.preview === "string" && o.preview.trim()) {
        lab.classList.add("has-preview");
        const { thumb, item, opens } = previewThumb(o.preview, o.label, s.cwd, req.request_id);
        item.picked = () => r.checked;
        item.choose = () => {
          r.checked = q.multiSelect ? !r.checked : true;
          r.dispatchEvent(new Event("change", { bubbles: true }));
        };
        // a file thumbnail is a link of its own (it opens the file); a click beside the link must not pick the option either
        // (a link with no address yet — the server still looks for the file, or it is not there — opens nothing either)
        if (opens) thumb.addEventListener("click", (e) => { if (!e.target.closest("a[href]")) e.preventDefault(); });
        else thumb.addEventListener("click", (e) => { e.preventDefault(); f.__box = openAskBox(shown, shown.indexOf(item), !!q.multiSelect); });
        shown.push(item);
        lab.append(r, txt, thumb);
      } else lab.append(r, txt);
      fs.append(lab);
    }
    // a custom answer is multiline: Enter answers, Shift+Enter inserts a line break; the field grows with the text
    const other = el("textarea", "other");
    other.rows = 1;
    other.name = "other" + i;
    other.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); f.requestSubmit(); }
    });
    other.addEventListener("input", () => { other.style.height = "auto"; other.style.height = `${other.scrollHeight + 2}px`; });
    other.placeholder = t("web.cards.other_ph");
    other.setAttribute("aria-label", t("web.cards.other_label", { question: q.question }));
    // without the shared "Answer" (a tap answers) a custom answer needs its own — on the same row as the field, when it has text
    // files for this answer: the chat field's attachments (attachments.js; paperclip, paste, drop, chips) under their own draft key
    // the card in the session's own chat and its copy the hub shows in its chat are two fields: s.name tells them apart
    const key = `ask:${s.name}:${asker}:${req.request_id}:${i}`;
    S.pending[key] = S.pending[key] || [];
    S.drafts[key] = "";
    other.addEventListener("input", () => { S.drafts[key] = other.value; });
    const chips = el("div", "pending ask-chips"), clip = el("button", "btn round quiet attach");
    clip.type = "button";
    clip.title = t("web.composer.attach_title");
    clip.setAttribute("aria-label", t("web.composer.attach_label"));
    clip.append(icon("attach"));
    clip.addEventListener("click", () => pickFiles(key));
    const draw = () => {
      chips.replaceChildren(...chipNodes(key, draw));
      chips.hidden = !S.pending[key].length;
      if (S.errors[key]) err.textContent = S.errors[key];
      const go = row.querySelector(".btn.primary");   // a tap answers; with only a file there is no text — the button must show
      if (go) go.hidden = !other.value.trim() && !S.pending[key].length;
    };
    boxes.set(key, { ta: other, refresh: draw, flash: () => {} });
    other.addEventListener("paste", (e) => {
      const files = droppedFiles(e.clipboardData);
      if (files.length) { e.preventDefault(); addFiles(key, files); }
    });
    fs.addEventListener("dragover", (e) => {
      if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); e.stopPropagation(); fs.classList.add("dragging"); }
    });
    fs.addEventListener("dragleave", (e) => { if (!fs.contains(e.relatedTarget)) fs.classList.remove("dragging"); });
    fs.addEventListener("drop", (e) => {
      fs.classList.remove("dragging");
      if (!e.dataTransfer?.files.length) return;
      e.preventDefault();
      e.stopPropagation();
      addFiles(key, droppedFiles(e.dataTransfer));
    });
    keys[i] = key;
    const row = el("div", "other-row"), mic = fieldMic(other);
    row.append(other, clip, mic);
    if (instant) {
      const go = el("button", "btn primary", t("web.cards.answer"));
      go.type = "submit";
      go.hidden = true;
      other.addEventListener("input", () => { go.hidden = !other.value.trim() && !S.pending[key].length; });
      row.append(go);
    }
    chips.hidden = true;
    fs.append(chips, row, mic.hubNote);   // a failed dictation: the reason and the kept recording, under the field
    f.append(fs);
  });
  const err = errorLine(), btns = el("div", "btns"), ok = button("primary", "allow", t(looked ? "web.cards.choose" : "web.cards.answer"), "submit");
  ok.hidden = instant;
  // a standard reply to any card: the session will answer in detail — screenshots, spec, diagrams
  const more = el("button", "btn more", t("web.cards.more_context"));
  more.type = "button";
  btns.append(ok, more);
  f.append(err, btns);
  // the line with the answer shows the button label in the page language, not the words meant for the session (MORE_CONTEXT)
  const said = (answers) => [...new Set(Object.values(answers).map((a) => {
    const [words, files] = a.split(new RegExp(`\\n?\\n?${FILES_HEAD}\\n`));
    const names = files ? files.split("\n").map((l) => l.replace(/^- /, "").replace(/ \(\[[^\]]*\]\)$/, "").split("/").pop()) : [];
    return [words === MORE_CONTEXT ? t("web.cards.more_context") : words, ...names].filter(Boolean).join(" · ");
  }))].join("; ");
  // files still uploading: the answer waits for them (as the chat field's send does)
  const uploading = () => keys.some((k) => (S.pending[k] || []).some((p) => !p.meta));
  const filesOf = (i) => S.pending[keys[i]] || [];
  // lost(why) — the core refused: the words of a comment from the session's field go back there (deliver)
  // marks — the labels picked per question (null — own words); none for "more context"
  const send = (answers, lost = null, marks = null) => {
    if (f.dataset.sending || f.dataset.frozen) return;   // an answer already on its way keeps its own lost (and its picks)
    f.__done?.();   // the session's field is no longer aimed at it (dock.js)
    const rid = req.request_id;
    picked.delete(rid);
    if (marks) picked.set(rid, { answers, marks });
    if (picked.size > PICKED_MAX) picked.delete(picked.keys().next().value);
    f.__lost = (why, open) => { picked.delete(rid); lost?.(why, open); };   // refused: not what the core recorded
    deliver(f, asker === s.name ? `${s.name}:req:${rid}` : `${s.name}:shown:${asker}:${rid}`,
      "/api/answer", { session: asker, request_id: rid, answers }, said(answers), (e) => {
        err.textContent = e;
        // tapping an option is the answer: the selection is cleared so that the same option can be tapped again (change)
        if (instant) for (const x of f.querySelectorAll("input[type=radio]:checked")) x.checked = false;
      });
  };
  more.addEventListener("click", () => {
    if (uploading()) { err.textContent = t("web.composer.wait_upload"); return; }
    send(Object.fromEntries(qs.map((q, i) => [q.question, withFiles(t("web.cards.more_context"), filesOf(i))])));
  });
  // tapping an option is the answer; keyboard arrows only switch the option (the browser fires change for them too —
  // an arrow used to submit the answer), Space and Enter answer
  let arrow = false;
  f.addEventListener("keydown", (e) => {
    arrow = e.key.startsWith("Arrow");
    if (instant && e.key === "Enter" && e.target.matches("input[type=radio]")) {
      e.preventDefault();
      e.target.checked = true;
      f.requestSubmit();
    }
  });
  f.addEventListener("keyup", () => { arrow = false; });
  f.addEventListener("change", (e) => {
    // the field is aimed at this card: the pick waits for the comment from the field and goes with it
    if (instant && !arrow && !f.classList.contains("aimed") && e.target.matches("input[type=radio]")) f.requestSubmit();
  });
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    if (f.dataset.frozen) return;
    if (uploading()) { err.textContent = t("web.composer.wait_upload"); return; }
    const answers = {}, marks = {};
    qs.forEach((q, i) => {
      // the "[Image #N]" marks a paste leaves in the field are not words: with a chosen option and no other text, the option is the answer
      const typed = f.elements["other" + i].value.trim(), own = typed.replace(/\[Image #\d+\]/g, "").trim() ? typed : "";
      const labels = [...f.querySelectorAll(`input[name="q${i}"]:checked`)].map((x) => x.value);
      answers[q.question] = withFiles(own || labels.join(", "), filesOf(i));
      marks[q.question] = own ? null : labels;
    });
    if (Object.values(answers).some((v) => !v)) { err.textContent = t("web.cards.pick_each"); return; }
    send(answers, null, marks);
  });
  // a comment written in the session's field while the field is aimed at this card (dock.js): it is the answer to
  // every question left without a pick; picks (and own answers typed in the card, as its own submit takes them) stay
  // as they are, and with every question picked it goes after the first one's. false — the card no longer takes an answer (sent, frozen); lost(why) — a later refusal of the core
  // the files attached to its answers and uploaded: a card taken off with them freezes, "To the field" takes them (dock.js)
  f.__files = () => keys.flatMap((k) => (S.pending[k] || []).filter((p) => p.meta));
  // the card is gone for good (answered — here or from another client, withdrawn, its session gone; dock.js): its answer's
  // files and draft keys go, so the page no longer counts them unsent; an open big view closes with it
  f.__drop = () => {
    for (const k of keys) { delete S.pending[k]; delete S.errors[k]; delete S.drafts[k]; boxes.delete(k); }
    f.__box?.close();
  };
  f.__comment = (text, lost) => {
    if (f.dataset.frozen || f.dataset.sending) return false;
    const own = qs.map((q, i) => f.elements["other" + i].value.trim());
    const labels = qs.map((q, i) => [...f.querySelectorAll(`input[name="q${i}"]:checked`)].map((x) => x.value));
    const picks = qs.map((q, i) => own[i] || labels[i].join(", "));
    const answers = Object.fromEntries(qs.map((q, i) => [q.question, picks[i] || text]));
    if (qs.length && picks.every(Boolean)) answers[qs[0].question] = `${picks[0]} — ${text}`;
    // files attached in the card go with it (those still uploading cannot: the comment does not wait)
    qs.forEach((q, i) => { answers[q.question] = withFiles(answers[q.question], filesOf(i).filter((p) => p.meta)); });
    send(answers, lost, Object.fromEntries(qs.map((q, i) => [q.question, own[i] || !labels[i].length ? null : labels[i]])));
    return true;
  };
  return f;
}

// what the session asks for — colored JSON: keys, strings, numbers in code-highlight colors; without the library — as text
function inputBox(input) {
  const box = el("div", "code");
  box.append(codeEl(JSON.stringify(input, null, 1), "json"));
  return box;
}

export function permCard(s, req) {
  const f = el("form", "card");
  const head = el("div", "card-head");
  head.append(el("h3", null, t("web.cards.perm_head", { tool: req.tool })));
  f.append(head);
  if (req.why) f.append(el("p", "card-why", t("web.cards.perm_why", { why: req.why })));   // a project rule, a sensitive path…
  f.append(inputBox(req.input));
  const why = reasonInput(), err = errorLine(), btns = el("div", "btns");
  const allow = button("primary", "allow", t("web.cards.allow")), deny = button("", "deny", t("web.cards.deny"));
  const act = (behavior, b) => deliver(f, `${s.name}:req:${req.request_id}`, "/api/permission",
    { session: s.name, request_id: req.request_id, behavior, message: why.value }, b.textContent, (e) => { err.textContent = e; });
  allow.addEventListener("click", () => act("allow", allow));
  deny.addEventListener("click", () => act("deny", deny));
  btns.append(allow, deny);
  f.append(why, err, btns);
  return f;
}
