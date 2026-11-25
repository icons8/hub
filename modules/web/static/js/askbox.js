// The big view of an option's preview in a question card (cards.js): a picture, an ASCII sketch or a file, paging through the
// card's options with ‹ › / ← →, and "Choose this" right there. A native <dialog>: the browser traps focus, Esc closes
// it (the page's own Esc, which closes a question, steps aside for an open dialog) and focus returns to the opener.
import { el, icon } from "./dom.js";
import { t } from "./i18n.js";

// an html report in a frame: sandboxed again on this side (scripts only — no popups, no top navigation), out of tab order
export function reportFrame(src) {
  const f = el("iframe");
  f.src = src;
  f.setAttribute("sandbox", "allow-scripts");
  f.setAttribute("loading", "lazy");
  f.setAttribute("referrerpolicy", "no-referrer");
  f.tabIndex = -1;
  f.title = "";
  return f;
}
export function fileFaceEl(name, dir) {
  const f = el("span", "opt-file-face");
  f.append(icon(dir ? "dir" : "file"), el("span", "opt-file-name", name));
  return f;
}

// items: [{label, kind: "img"|"text"|"file"|"missing", src?, text?, name?, dir?, where?, picked(): bool, choose(): void}];
// a file (where: a promise of {href, render}): an html report is its live frame, anything else its icon and name; "Open"
// takes it to a new tab; {} — the file is not there.
// multi — choosing toggles and keeps the view open (checkboxes); otherwise choosing closes it
export function openAskBox(items, at, multi) {
  const dlg = el("dialog", "askbox"), head = el("div", "askbox-head"), body = el("div", "askbox-body"), foot = el("div", "askbox-foot");
  const nav = (cls, label, sign, d) => {
    const b = el("button", "askbox-btn " + cls, sign);
    b.type = "button";
    b.title = b.ariaLabel = label;
    b.addEventListener("click", () => go(d));
    return b;
  };
  const prev = nav("prev", t("web.cards.box_prev"), "‹", -1), next = nav("next", t("web.cards.box_next"), "›", 1);
  const title = el("span", "askbox-title"), count = el("span", "askbox-count");
  const close = el("button", "askbox-btn close", t("web.cards.box_close"));
  close.type = "button";
  close.addEventListener("click", () => dlg.close());
  const pick = el("button", "askbox-pick");
  pick.type = "button";
  // a file's "Open": a link to a new tab (the report whole, a pdf, a folder in "Files"); hidden for other previews
  const open = el("a", "askbox-btn askbox-open", t("web.cards.box_open"));
  open.target = "_blank";
  open.rel = "noopener";
  head.append(prev, next, title, count);
  foot.append(pick, open, close);
  dlg.append(head, body, foot);
  dlg.setAttribute("aria-label", t("web.cards.box_label"));
  const many = items.length > 1;
  prev.hidden = next.hidden = count.hidden = !many;
  const draw = () => {
    const it = items[at];
    open.hidden = true;
    open.removeAttribute("href");
    title.textContent = it.label;
    count.textContent = `${at + 1} / ${items.length}`;
    let node;
    if (it.kind === "img") { node = el("img", "askbox-img"); node.alt = it.label; node.src = it.src; }
    else if (it.kind === "text") { node = el("pre", "askbox-pre"); node.textContent = it.text; node.tabIndex = 0; }
    else if (it.kind === "file") {
      node = el("div", "askbox-file");
      node.append(fileFaceEl(it.name, it.dir));
      it.where.then((w) => {
        if (items[at] !== it) return;   // paged on meanwhile
        if (!w.href) { node.append(el("p", "askbox-missing", t("web.cards.file_missing"))); return; }
        if (w.render) { const f = reportFrame(w.render); f.className = "askbox-frame"; node.replaceChildren(f); }
        open.href = w.href;
        open.hidden = false;
      });
    }
    else node = el("p", "askbox-missing", it.text);
    body.replaceChildren(node);
    body.scrollTo(0, 0);
    const on = it.picked();
    pick.textContent = on && multi ? t("web.cards.box_unpick") : on ? t("web.cards.box_picked") : t("web.cards.box_pick");
    pick.classList.toggle("on", on);
  };
  const go = (d) => { at = (at + d + items.length) % items.length; draw(); };
  pick.addEventListener("click", () => { items[at].choose(); if (multi) draw(); else dlg.close(); });
  dlg.addEventListener("keydown", (e) => {
    if (!many || e.altKey || e.ctrlKey || e.metaKey || e.target.closest?.("pre")) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); go(-1); }
    if (e.key === "ArrowRight") { e.preventDefault(); go(1); }
  });
  // the dim area around closes it; the box's own padding and the gaps between its rows are the dialog too: not those
  dlg.addEventListener("click", (e) => {
    if (e.target !== dlg) return;
    const r = dlg.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dlg.close();
  });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  draw();
  dlg.showModal();
  pick.focus();
  return dlg;
}
