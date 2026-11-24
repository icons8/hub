// Settings → Secrets (spec d2345345 §3.3): the hub's own vault. The owner adds, changes, removes and copies secrets;
// agents put them into form fields of their browser and into commands' environment without seeing them
// (`hub secret fill|exec`). The state comes from GET /api/secrets, not from the snapshot, and has no values at all: a
// value reaches the page only as the answer of "Copy" (POST /api/secrets/copy) and is never drawn — agents take
// screenshots of the page. Through Cloudflare Access values travel neither way (`values: false`): the value field and
// "Copy" are not there. A session may put a value in itself (`hub secret add`, spec d2441132): the card names it and the
// file, and "Change" lists the previous values with "Return" — the boxes move sealed, so that works everywhere.
import { el } from "./dom.js";
import { clock } from "./clock.js";
import { post } from "./api.js";
import { copyText } from "./markdown.js";
import { confirmDlg } from "./dialogs.js";
import { locale, t } from "./i18n.js";

const AGAIN = 60000;   // the tab is drawn on every snapshot: the state is asked again no more often than this
const HOLD = 20000;    // a value the browser would not copy after the request waits this long for a second press
let root = null, info = null, asked = 0, form = null, error = "", said = "";
let held = null;   // {name, field, value, timer}: the value of a refused copy, until the second press or HOLD

function button(label, cls, onClick) {
  const b = el("button", "btn small " + (cls || ""), label);
  b.type = "button";
  b.addEventListener("click", async () => { b.disabled = true; try { await onClick(b); } finally { b.disabled = false; } });
  return b;
}

async function load(force = false) {
  if (!force && Date.now() - asked < AGAIN) return;
  asked = Date.now();
  try {
    const r = await fetch("/api/secrets", { headers: { "X-Hub": "1" } });
    info = r.ok ? await r.json() : null;
  } catch {
    info = null;
  }
  draw();
}

async function act(what, body) {
  const r = await post("/api/secrets/" + what, body);
  error = r.error || "";
  return r;
}

function forget() {
  if (held) clearTimeout(held.timer);
  held = null;
}

// "Copy": in a secure context the clipboard takes the request's promise in the same press (Safari keeps the gesture);
// otherwise the value is copied right after the answer; a browser that refuses that gets a second press, which copies
// at once from memory. The value is never put into the page.
async function copy(name, field) {
  error = said = "";
  if (held && held.name === name && held.field === field) {
    const value = held.value;
    forget();
    try { await copyText(value); said = t("web.secrets.copied"); } catch { error = t("web.secrets.copy_failed"); }
    return draw();
  }
  forget();
  const req = act("copy", { name, field });
  if (window.isSecureContext && navigator.clipboard?.write && window.ClipboardItem) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ "text/plain": req.then((r) => {
        if (r.error) throw new Error(r.error);
        return new Blob([r.data.value], { type: "text/plain" });
      }) })]);
      said = t("web.secrets.copied");
      return draw();
    } catch { /* below: by the answer, or a second press */ }
  }
  const r = await req;
  if (r.error) return draw();
  try {
    await copyText(r.data.value);
    said = t("web.secrets.copied");
  } catch {
    held = { name, field, value: r.data.value, timer: setTimeout(() => { held = null; draw(); }, HOLD) };
  }
  draw();
}

function ago(ts) {
  const d = new Date(typeof ts === "number" ? ts * 1000 : ts);   // the fold keeps the event's time in seconds
  if (Number.isNaN(d.getTime())) return "";
  return clock(d.getTime() / 1000, locale, { day: "numeric", month: "short" });
}

function lastLine(last) {
  if (!last) return t("web.secrets.never_used");
  const when = ago(last.ts), who = last.who || "";
  if (last.how === "copy") return t("web.secrets.last_copy", { when });
  const where = last.where || "";
  const key = last.outcome === "pending" ? "web.secrets.last_pending" : last.outcome === "failed" ? "web.secrets.last_failed"
    : last.how === "exec" ? "web.secrets.last_exec" : "web.secrets.last_fill";
  return t(key, { when, who, where });
}

// who set a value: "" — the owner on this page, ":terminal" — `hub secret add` without a session, else a session
function whoName(who) {
  if (!who) return t("web.secrets.who_you");
  return who === ":terminal" ? t("web.secrets.who_terminal") : t("web.secrets.who_session", { name: who });
}

// where a value came from: a file and its line as "path (KEY)", "stdin" as it is
function sourceText(by) {
  return by.key ? `${by.source || ""} (${by.key})` : by.source || "";
}

// the line under a secret whose value a session put in or changed; none once the owner set a value or returned one here
// (a note or a switch the owner edits leaves the value the session's)
function byLine(m) {
  if (!m.by || !m.by.who || m.by.restored) return null;
  return el("p", "ac-note ac-muted", t(m.prev && m.prev.length ? "web.secrets.changed_by" : "web.secrets.added_by",
    { who: whoName(m.by.who), source: sourceText(m.by), when: ago(m.by.ts || m.changed) }));
}

// "Change": the previous values (up to three), each with "Return"; the current one goes to them in its turn
function previous(m) {
  const box = el("div", "sec-prev");
  box.append(el("p", "ac-note", t("web.secrets.prev_title")));
  for (const p of m.prev) {
    const when = ago(p.changed), who = whoName(p.by && p.by.who);
    const row = el("div", "sec-row");
    row.append(el("span", "ac-muted", p.by && p.by.source ? t("web.secrets.prev_row_from", { when, who, source: sourceText(p.by) })
      : t("web.secrets.prev_row", { when, who })));
    row.append(button(t("web.secrets.restore"), "", () => {
      confirmDlg(t("web.secrets.restore_title", { when }), t("web.secrets.restore_text"), t("web.secrets.restore"), async () => {
        const r = await act("restore", { name: m.name, id: p.id });
        if (r.error) return draw();
        form = null;
        said = t("web.secrets.restored");
        await load(true);
      }, "");   // not a removal: an ordinary button
    }));
    box.append(row);
  }
  return box;
}

// catalog text with `commands` in backticks — <code> nodes for them
function withCode(text) {
  return text.split("`").map((part, i) => (i % 2 ? el("code", null, part) : document.createTextNode(part)));
}

function card(m) {
  const c = el("div", "ac-card sec-card");
  const head = el("div", "ac-head"), title = el("div", "ac-title");
  title.append(el("span", "ac-name", m.name), el("span", "ac-kind", m.login ? t("web.secrets.with_login") : ""));
  head.append(title);
  if (m.commands) head.append(el("span", "ac-badge", t("web.secrets.commands_badge")));
  c.append(head);
  c.append(el("p", "ac-note", m.sites.length ? t("web.secrets.sites", { sites: m.sites.join(", ") }) : t("web.secrets.no_sites")));
  if (m.note) c.append(el("p", "ac-note", m.note));
  const by = byLine(m);
  if (by) c.append(by);
  c.append(el("p", "ac-note ac-muted", m.value ? lastLine(m.last) : t("web.secrets.no_value")));
  const row = el("div", "sec-row");
  if (m.value && info.values) {
    const again = (f) => held && held.name === m.name && held.field === f;
    row.append(button(again("password") ? t("web.secrets.copy_again") : t("web.secrets.copy"), again("password") ? "primary" : "",
      () => copy(m.name, "password")));
    if (m.login) row.append(button(again("username") ? t("web.secrets.copy_again") : t("web.secrets.copy_login"),
      again("username") ? "primary" : "", () => copy(m.name, "username")));
  }
  row.append(button(t("web.secrets.edit"), "", () => { form = { name: m.name, isNew: false, m }; error = said = ""; draw(); }));
  row.append(button(t("web.secrets.remove"), "", () => {
    confirmDlg(t("web.secrets.remove_title", { name: m.name }), t("web.secrets.remove_text"), t("web.secrets.remove"),
      async () => { const r = await act("remove", { name: m.name }); if (!r.error) await load(true); else draw(); });
  }));
  c.append(row);
  if (held && held.name === m.name) c.append(el("p", "hint", t("web.secrets.press_again")));
  return c;
}

function field(label, input, hint) {
  const l = el("label", "sec-field");
  l.append(el("span", null, label), input);
  if (hint) l.append(el("small", "ac-muted", hint));
  return l;
}

function editor() {
  const f = el("form", "ac-card sec-form");
  f.autocomplete = "off";
  const m = form.m || { name: "", sites: [], commands: false, note: "", login: false };
  const name = el("input");
  name.value = m.name;
  name.disabled = !form.isNew;
  name.maxLength = 40;
  name.spellcheck = false;
  const login = el("input");
  login.autocomplete = "off";
  login.placeholder = form.isNew ? "" : (m.login ? t("web.secrets.keep") : "");
  const value = el("input");
  value.type = "password";
  value.autocomplete = "new-password";
  value.placeholder = form.isNew ? "" : t("web.secrets.keep");
  const sites = el("textarea");
  sites.rows = 2;
  sites.spellcheck = false;
  sites.value = m.sites.join("\n");
  const commands = el("input");
  commands.type = "checkbox";
  commands.checked = !!m.commands;
  const note = el("input");
  note.maxLength = 200;
  note.value = m.note || "";
  f.append(el("h4", null, form.isNew ? t("web.secrets.add") : t("web.secrets.edit_title", { name: m.name })),
    field(t("web.secrets.name"), name, t("web.secrets.name_hint")));
  if (info.values) {
    f.append(field(t("web.secrets.login"), login), field(t("web.secrets.value"), value));
  } else f.append(el("p", "hint warn", t("web.secrets.no_values_remote")));
  f.append(field(t("web.secrets.sites_label"), sites, t("web.secrets.sites_hint")));
  const opt = el("label", "opt"), txt = el("span");
  txt.append(document.createTextNode(t("web.secrets.commands")), el("small", null, t("web.secrets.commands_hint")));
  opt.append(commands, txt);
  f.append(opt, field(t("web.secrets.note"), note));
  if (!form.isNew && m.prev && m.prev.length) f.append(previous(m));
  const save = el("button", "btn small primary", t("web.secrets.save"));
  save.type = "submit";
  const cancel = button(t("web.secrets.cancel"), "", () => { form = null; error = ""; draw(); });
  const row = el("div", "sec-row");
  row.append(save, cancel);
  f.append(row);
  if (error) f.append(el("p", "hint warn", error));
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    save.disabled = true;
    const body = { name: name.value.trim(), sites: sites.value.split(/\s+/).filter(Boolean), commands: commands.checked,
                   note: note.value.trim(), new: form.isNew };
    if (info.values && value.value) body.password = value.value;
    if (info.values && login.value.trim()) body.login = login.value.trim();   // empty: a new one without, an old one as it is
    const r = await act("save", body);
    value.value = "";
    save.disabled = false;
    if (r.error) return draw();
    form = null;
    said = t("web.secrets.saved");
    await load(true);
  });
  return f;
}

function draw() {
  if (!root) return;
  if (!info) return void root.replaceChildren();
  const sec = el("section", "settings-sec ac secrets");
  const intro = el("p", "ac-sum");
  intro.append(...withCode(t("web.secrets.intro")));
  sec.append(intro);
  const warn = { no_lib: "web.secrets.no_lib", lost: "web.secrets.lost", key_inside: "web.secrets.key_inside",
                 broken: "web.secrets.broken" }[info.state];
  if (!info.on) sec.append(el("p", "hint warn", t("web.secrets.off")));
  if (warn) {
    const p = el("div", "sec-more");
    p.append(el("p", "hint warn", t(warn)));
    if (info.state === "no_lib") p.append(button(t("web.secrets.install"), "primary", async (b) => {
      b.textContent = t("web.secrets.installing");
      const r = await act("install", {});
      if (!r.error) await load(true); else draw();
    }));
    if (info.state === "lost") p.append(button(t("web.secrets.start_over"), "", () => {
      confirmDlg(t("web.secrets.start_over"), t("web.secrets.start_over_text"), t("web.secrets.start_over"),
        async () => { const r = await act("start-over", {}); if (!r.error) await load(true); else draw(); });
    }));
    sec.append(p);
  }
  if (!info.values) sec.append(el("p", "hint warn", t("web.secrets.no_values_remote")));
  // the hubs of this account without a page password (spec d2344898 §2.0): anyone on this computer opens their page
  if (Array.isArray(info.unlocked) && info.unlocked.length) {
    const p = el("p", "hint warn");
    const go = el("a", null, t("web.secrets.set_password"));
    go.href = "#/settings";
    go.addEventListener("click", () => dispatchEvent(new CustomEvent("hub-open-settings", { detail: "general" })));
    p.append(document.createTextNode(t("web.secrets.unlocked", { names: info.unlocked.join(", ") }) + " "), go);
    sec.append(p);
  }
  if (info.state === "ok" || info.state === "lost") {
    const top = el("div", "sec-row");
    if (!form && info.state === "ok") top.append(button(t("web.secrets.add"), "primary", () => { form = { isNew: true }; error = said = ""; draw(); }));
    if (said) top.append(el("span", "ac-muted", said));
    sec.append(top);
    if (form) sec.append(editor());
    if (error && !form) sec.append(el("p", "hint warn", error));
    const list = el("div", "ac-list");
    list.append(...info.items.map(card));
    if (info.items.length) sec.append(list);
    else if (!form) sec.append(el("p", "ac-note", t("web.secrets.empty")));
  }
  sec.append(el("p", "ac-note", t("web.secrets.boundary")));
  root.replaceChildren(sec);
}

export function secretsSection() {
  root = el("div", "secrets-wrap");
  load(true);
  return root;
}

// the tab is refreshed on every snapshot: ask again now and then, never while the form is open or a value is held
export function refreshSecrets() {
  if (!form && !held && !(root && root.contains(document.activeElement))) load();
}
