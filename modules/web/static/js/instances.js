// "Settings -> Main -> Hubs on this computer" (modules/web/instancesweb.py; the spec of several hubs, 2.5, 2.6): the
// hubs of this machine — the main one and every further one, each its own sessions, journal and settings — with
// "Open", "Remove" and "Password" (the page's password of each hub, core/pagepass.py: set, changed, taken off in its
// row; "Sign out" in this hub's own); "Add a hub" sits in the block's "More" (the owner's choice 9.1: whoever does not
// look for it does not see it). The install runs apart from the page's server (core/instancejob.py): the page writes nothing
// itself, it asks GET /api/instances again while the job runs and shows its end. A page server without it (older,
// before its restart) answers 404 — no block.
import { el } from "./dom.js";
import { post } from "./api.js";
import { t } from "./i18n.js";
import { keepOpen } from "./keepopen.js";

const AGAIN = 10000;   // the tab is drawn on every snapshot: the list is asked again no more often than this
const RUNNING = 1500;  // while a job runs
let root = null, info = null, asked = 0, timer = null;
let error = { text: "", at: "" };   // the last refusal and where it is said: under the form, the name to remove, the job
const draft = { name: "", engine: null, claude: "shared" };   // the form across redraws
let removing = null, typed = "", bye = "";   // the hub whose "Remove" is open and the name typed to confirm it
let passFor = null;   // the hub whose "Password" form is open (its name; "" — the main hub)
let listBox = null, jobBox = null, more = null, form = null, formSig = "";

function button(label, cls, onClick) {
  const b = el("button", "btn small " + (cls || ""), label);
  b.type = "button";
  b.addEventListener("click", async () => { b.disabled = true; try { await onClick(); } finally { b.disabled = false; } });
  return b;
}

async function load(force = false) {
  if (!force && Date.now() - asked < AGAIN) return;
  asked = Date.now();
  try {
    const r = await fetch("/api/instances", { headers: { "X-Hub": "1" } });
    info = r.ok ? await r.json() : null;
  } catch {
    info = null;
  }
  const ended = info?.job?.name === bye && ["failed", "lost"].includes(info.job.state);
  if (ended) bye = "";   // removing this hub did not go through: it stays, and so does its page
  draw();
  // other hubs start and stop without an event of this one: while the block is on the screen it asks again by itself,
  // often while a job runs; a block no longer on the page stops asking
  clearTimeout(timer);
  const running = info?.job?.state === "running";
  timer = setTimeout(() => { if (root?.isConnected) load(running); }, running ? RUNNING : AGAIN);
}

async function act(op, body = {}) {
  const r = await post("/api/instances", { op, ...body });
  error = { text: r.error || "", at: op };
  await load(true);
  return r;
}

// the other hub's page: the same host as this page, its own port (the tailnet answers on every hub's port); a page
// behind a proxy without a port — no link, the command says how to open it
function urlOf(port) {
  if (!port || !location.port) return "";
  return `${location.protocol}//${location.hostname}:${port}/`;
}

function engineName(e) {
  return e === "codex" ? "Codex" : "Claude Code";
}

function hubRow(h) {
  const row = el("div", "inst-row" + (h.running ? "" : " off"));
  row.dataset.hub = h.name || "";
  const dot = el("span", "inst-dot");
  dot.title = t(h.running ? "web.instances.running" : "web.instances.stopped");
  const who = el("div", "inst-who"), head = el("div", "inst-name");
  head.append(el("b", null, h.title || h.name || t("web.instances.main_title")));
  if (h.name && h.title !== h.name) head.append(el("span", "chip", h.name));
  if (h.main) head.append(el("span", "chip", t("web.instances.main")));
  if (h.me) head.append(el("span", "chip ok", t("web.instances.this")));
  const lock = { password: "web.pass.chip_password", key: "web.pass.chip_key", broken: "web.pass.chip_broken" }[h.signin];
  if (lock) head.append(el("span", "chip inst-lock" + (h.signin === "broken" ? " warn" : ""), t(lock)));
  const bits = [h.command, h.port ? `127.0.0.1:${h.port}` : "", engineName(h.engine)];
  if (h.engine !== "codex" && h.claude === "own") bits.push(t("web.instances.claude_own"));
  if (h.gone) bits.push(t("web.instances.gone"));
  else if (h.port_busy) bits.push(t("web.instances.port_busy", { port: h.port }));
  else if (!h.running) bits.push(t("web.instances.stopped"));
  const meta = el("div", "inst-meta", bits.filter(Boolean).join(" · "));
  who.append(head, meta);
  const acts = el("div", "inst-acts");
  const url = !h.me && h.running ? urlOf(h.port) : "";
  if (url) {
    const a = el("a", "btn small", t("web.instances.open"));
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    acts.append(a);
  }
  if (passFor !== h.name) {
    const b = button(t("web.pass.button"), "quiet inst-pass-btn", async () => {
      passFor = h.name; removing = null; error = { text: "", at: "" }; draw();
      root?.querySelector(".inst-pass input")?.focus();
    });
    if (h.password_ok === false) { b.disabled = true; b.title = t("web.pass.old_code"); }
    acts.append(b);
  }
  if (h.me && h.signin === "password") {
    const out = button(t("web.pass.sign_out"), "quiet", async () => {
      const r = await post("/api/signout", {});
      if (!r.error) location.reload();
    });
    out.title = t("web.pass.sign_out_hint");
    acts.append(out);
  }
  if (!h.main && removing !== h.name) acts.append(button(t("web.instances.remove"), "quiet inst-remove", async () => {
    removing = h.name; passFor = null; typed = ""; draw();
    root?.querySelector(".inst-confirm input")?.focus();
  }));
  row.append(dot, who, acts);
  if (removing === h.name) row.append(confirmRow(h));
  if (passFor === h.name) row.append(passwordRow(h));
  return row;
}

function field(label, cls, name, auto) {
  const wrap = el("label", "inst-field " + cls);
  const f = el("input", "inst-input");
  f.type = "password";
  f.name = name;
  f.autocomplete = auto;
  f.maxLength = 256;
  f.spellcheck = false;
  wrap.append(el("span", null, label), f);
  return { wrap, f };
}

// "Password": the page's password of this hub (core/pagepass.py) — set; or, when it has one, changed or taken off
// with the current one (one hub's page proves nothing about another; an unattended tab cannot change it)
function passwordRow(h) {
  const box = el("div", "inst-confirm inst-pass");
  const has = h.signin === "password";
  const cancel = button(t("web.instances.cancel"), "quiet", () => { passFor = null; error = { text: "", at: "" }; draw(); });
  if (h.signin === "broken" || (h.signin === "key" && !h.me)) {
    box.append(el("p", "hint warn", t(h.signin === "broken" ? "web.pass.broken" : "web.pass.key_elsewhere",
      { cmd: h.command || "hub" })), cancel);
    return box;
  }
  box.append(el("p", "hint", t(has ? "web.pass.change_hint" : "web.pass.set_hint", { min: 8 })));
  const cur = has ? field(t("web.pass.current"), "cur", "current-password", "current-password") : null;
  const neu = field(t("web.pass.new"), "new", "new-password", "new-password");
  const show = button(t("web.pass.show"), "quiet", () => {
    const hidden = neu.f.type === "password";
    neu.f.type = hidden ? "text" : "password";
    show.textContent = t(hidden ? "web.pass.hide" : "web.pass.show");
  });
  const err = el("p", "hint warn");
  err.hidden = true;
  // the list is not rebuilt while the owner is in it: the refusal goes under the form at once
  const done = (r) => {
    if (!r.error) { passFor = null; error = { text: "", at: "" }; draw(); return; }
    err.textContent = r.error;
    err.hidden = false;
  };
  const go = button(t(has ? "web.pass.change" : "web.pass.set"), "primary", async () => {
    const r = await act("password", { name: h.name, new: neu.f.value, ...(cur ? { current: cur.f.value } : {}) });
    done(r);
  });
  const line = el("div", "inst-line");
  line.append(...(cur ? [cur.wrap] : []), neu.wrap, show, go);
  if (has) {
    line.append(button(t("web.pass.take_off"), "danger", async () => {
      const r = await act("password_off", { name: h.name, current: cur.f.value });
      done(r);
    }));
  }
  line.append(cancel);
  for (const f of [cur?.f, neu.f].filter(Boolean)) {
    f.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); go.click(); }
      if (e.key === "Escape") { e.preventDefault(); passFor = null; draw(); }
    });
  }
  box.append(line, err);
  return box;
}

// a password holds only when every hub of this account has one: a hub without it opens the others (its "Files" read
// the whole disk, its sessions run as the same account) — the server's list (core/pagepass.unprotected), said once
// some hub has a password
function openOthers(hubs, open) {
  if (!hubs.some((h) => h.signin === "password") || !open?.length) return null;
  const title = (name) => { const h = hubs.find((x) => x.name === name); return h?.title || name || t("web.instances.main_title"); };
  return el("p", "hint warn inst-open", t("web.pass.all_or_none", { names: open.map(title).join(", ") }));
}

// "Remove" asks for the name — or, for a hub with a password, its password: a whole hub goes (its service and command;
// its data stays in its folder)
function confirmRow(h) {
  const box = el("div", "inst-confirm");
  const pass = h.signin === "password";
  box.append(el("p", "hint", t(h.me ? "web.instances.remove_me" : "web.instances.remove_hint", { name: h.name })));
  if (pass) box.append(el("p", "hint", t("web.pass.remove_hint")));
  const f = el("input", "inst-input");
  f.type = pass ? "password" : "text";
  f.maxLength = pass ? 256 : 40;
  f.value = typed;
  f.placeholder = pass ? "" : h.name;
  f.setAttribute("aria-label", t(pass ? "web.pass.current" : "web.instances.remove_label", { name: h.name }));
  f.autocomplete = pass ? "current-password" : "off";
  f.spellcheck = false;
  const ready = () => (pass ? f.value.length > 0 : f.value.trim() === h.name);
  const go = button(t("web.instances.remove_go"), "danger", async () => {
    const r = await act("remove", { name: h.name, ...(pass ? { password: f.value } : { confirm: f.value.trim() }) });
    if (!r.error) { removing = null; typed = ""; if (h.me) bye = h.name; draw(); }
  });
  go.disabled = !ready();
  f.addEventListener("input", () => { typed = pass ? "" : f.value; go.disabled = !ready(); });
  f.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !go.disabled) { e.preventDefault(); go.click(); }
    if (e.key === "Escape") { e.preventDefault(); removing = null; draw(); }
  });
  const row = el("div", "inst-line");
  row.append(f, go, button(t("web.instances.cancel"), "quiet", () => { removing = null; error = { text: "", at: "" }; draw(); }));
  box.append(row);
  if (error.text && error.at === "remove") box.append(el("p", "hint warn", error.text));
  return box;
}

function jobNode(job) {
  if (!job) return null;
  const box = el("div", "inst-job " + job.state);
  const key = { running: job.op === "new" ? "web.instances.adding" : "web.instances.removing",
    done: job.op === "new" ? "web.instances.added" : "web.instances.removed",
    failed: job.op === "new" ? "web.instances.add_failed" : "web.instances.remove_failed",
    lost: "web.instances.lost" }[job.state];
  box.append(el("p", null, t(key, { name: job.name })));
  if (job.state === "done" && job.op === "new") {
    const h = (info?.hubs || []).find((x) => x.name === job.name);
    const url = h && urlOf(h.port);
    if (url) {
      const a = el("a", "btn small primary", t("web.instances.open_new"));
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      box.append(a);
    }
    if (h?.claude === "own" && h.engine !== "codex") box.append(el("p", "hint", t("web.instances.sign_in_there")));
    if (h?.engine === "codex") box.append(el("p", "hint", t("web.instances.codex_sign_in_there")));
  }
  if (job.state !== "running" && job.state !== "done" && job.lines?.length) box.append(el("pre", "inst-log", job.lines.join("\n")));
  if (job.state !== "running") box.append(button(t("web.instances.dismiss"), "quiet", () => act("clear")));
  return box;
}

function seg(options, value, set) {
  const s = el("div", "seg");
  for (const [v, label] of options) {
    const b = el("button", null, label);
    b.type = "button";
    b.dataset.v = v;
    b.setAttribute("aria-pressed", String(v === value));
    b.addEventListener("click", () => { set(v); for (const x of s.children) x.setAttribute("aria-pressed", String(x === b)); });
    s.append(b);
  }
  return s;
}

// the form is built once and kept (the cursor and the typed name stay through the snapshots); rebuilt only when what
// it offers changes
function buildForm() {
  const sig = JSON.stringify([info?.add, info?.engines, info?.engine, info?.codex_found]);
  if (form && sig === formSig) return form;
  formSig = sig;
  form = el("div", "inst-add");
  if (info?.add) {
    form.append(el("p", "hint warn", info.add));
    return form;
  }
  const engines = info?.engines?.length ? info.engines : ["claude"];
  draft.engine = engines.includes(draft.engine) ? draft.engine : engines.includes(info?.engine) ? info.engine : engines[0];
  const name = el("input", "inst-input inst-new-name");
  name.type = "text";
  name.maxLength = 16;
  name.value = draft.name;
  name.placeholder = t("web.instances.name_ph");
  name.autocomplete = "off";
  name.spellcheck = false;
  name.setAttribute("aria-label", t("web.instances.name"));
  const nameBox = el("label", "inst-field");
  nameBox.append(el("span", null, t("web.instances.name")), name);
  const login = el("div", "inst-field");
  login.append(el("span", null, t("web.instances.claude")),
    seg([["shared", t("web.instances.claude_shared")], ["own", t("web.instances.claude_own_choice")]], draft.claude,
      (v) => { draft.claude = v; }));
  const engineBox = el("div", "inst-field");
  // under Codex: where the texts go (decided 9.5), and, with no codex on the machine, that the hub downloads its own
  const codexNote = el("div", "inst-codex");
  codexNote.append(el("p", "hint", t("web.instances.codex_note")));
  if (info?.codex_found === false) codexNote.append(el("p", "hint", t("web.instances.codex_download")));
  const showEngine = () => { login.hidden = draft.engine === "codex"; codexNote.hidden = draft.engine !== "codex"; };
  if (engines.length > 1) {
    engineBox.append(el("span", null, t("web.instances.engine")),
      seg(engines.map((e) => [e, engineName(e)]), draft.engine, (v) => { draft.engine = v; showEngine(); }));
  }
  // its own press handling, not button(): after the press the state (a name typed, a job running) decides, not "enabled"
  const go = el("button", "btn small primary", t("web.instances.add_go"));
  go.type = "button";
  let busy = false;
  go.addEventListener("click", async () => {
    busy = true; sync();
    try {
      const r = await act("new", { name: name.value.trim(), engine: draft.engine, claude: draft.claude });
      if (!r.error) { draft.name = ""; name.value = ""; }
    } finally { busy = false; sync(); }
  });
  const sync = () => { draft.name = name.value; go.disabled = busy || !name.value.trim() || info?.job?.state === "running"; };
  name.addEventListener("input", sync);
  name.addEventListener("keydown", (e) => { if (e.key === "Enter" && !go.disabled) { e.preventDefault(); go.click(); } });
  sync();
  form.sync = sync;
  const line = el("div", "inst-line");
  line.append(nameBox, ...(engines.length > 1 ? [engineBox] : []), login, go);
  form.err = el("p", "hint warn");
  form.append(el("b", null, t("web.instances.add_title")), line, form.err, codexNote, el("p", "hint", t("web.instances.add_hint")));
  showEngine();
  return form;
}

function draw() {
  if (!root) return;
  // this very hub is being removed: its server goes, the page cannot ask any more — say so instead of an empty place
  if (!info && bye) { listBox = null; return void root.replaceChildren(el("p", "hint warn", t("web.instances.removed_me", { name: bye }))); }
  if (!info) { listBox = null; return void root.replaceChildren(); }
  if (!listBox) {
    listBox = el("div", "inst-list");
    jobBox = el("div", "inst-jobs");
    more = el("details", "set-more inst-more");
    keepOpen(more, "general:instances");
    more.append(el("summary", null, t("web.instances.more")));
    const sec = el("section", "settings-sec instances");
    sec.append(el("h3", "day", t("web.instances.title")), el("p", "hint", t("web.instances.hint")), listBox, jobBox, more);
    root.replaceChildren(sec);
  }
  // the list of rows is rebuilt unless the owner is typing in it (the name to remove one, a password)
  if (!((removing !== null || passFor !== null) && listBox.contains(document.activeElement))) {
    const warn = openOthers(info.hubs || [], info.unprotected);
    listBox.replaceChildren(...(info.hubs || []).map(hubRow), ...(warn ? [warn] : []));
  }
  const job = jobNode(info.job);
  const said = (at) => (error.text && error.at === at ? [el("p", "hint warn", error.text)] : []);
  jobBox.replaceChildren(...(job ? [job] : []), ...said("clear"));
  const f = buildForm();
  if (f.parentNode !== more) { more.querySelector(".inst-add")?.remove(); more.append(f); }
  f.sync?.();
  if (f.err) { f.err.textContent = error.at === "new" ? error.text : ""; f.err.hidden = !f.err.textContent; }
}

export function instancesSection() {
  root = el("div", "instances-wrap");
  listBox = jobBox = more = form = null;
  formSig = "";
  load(true);
  return root;
}

// the tab is refreshed on every snapshot: ask again now and then, never while the owner types in the block
export function refreshInstances() {
  if (!(root && root.contains(document.activeElement) && document.activeElement.tagName === "INPUT")) load();
}
