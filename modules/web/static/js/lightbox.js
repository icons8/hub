// Feed images shown large over the page (previews inline in the chat, click to open a lightbox).
// Opens on a click on any feed image: thumbnails of message links (.thumbs), attachments, received files, a step's
// image; pages through the images of the same message. Esc, ✕ or a click outside closes; ← → and the buttons page;
// on a phone, swipe. The image fits the window; a click on it shows it at natural size with scrolling, another click
// fits it to the window again.
import { el, icon } from "./dom.js";
import { t, dir } from "./i18n.js";

const PICK = ".feed .thumbs img, .feed .attach-list img, .feed .tool-img, .feed .opt-img";
let box = null, list = [], at = 0;

function build() {
  box = el("div", "lightbox");
  box.hidden = true;
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-label", t("web.lightbox.image"));
  const img = el("img", "lb-img");
  img.alt = "";
  const btn = (cls, label, ico, fn) => {
    const b = el("button", "lb-btn " + cls);
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.title = label;
    b.append(typeof ico === "string" ? document.createTextNode(ico) : ico);
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    return b;
  };
  const count = el("span", "lb-count");
  const name = el("span", "lb-name");
  const bar = el("div", "lb-bar");
  const open = el("a", "lb-open", t("web.lightbox.open_file"));
  open.target = "_blank";
  open.rel = "noopener";
  open.addEventListener("click", (e) => e.stopPropagation());
  bar.append(count, name, open);
  box.append(img, btn("lb-prev", t("web.lightbox.prev"), "‹", () => go(-1)), btn("lb-next", t("web.lightbox.next"), "›", () => go(1)),
    btn("lb-close", t("web.lightbox.close"), icon("close"), close), bar);
  box.addEventListener("click", (e) => { if (e.target === box) close(); });   // outside the image
  img.addEventListener("click", (e) => {   // on the image — as is (so a before/after screenshot can be read whole), again — fit to window
    e.stopPropagation();
    box.classList.toggle("zoom");
    box.scrollTo(0, 0);
  });
  let x0 = null;   // swipe on a phone
  box.addEventListener("pointerdown", (e) => { x0 = e.pointerType === "mouse" ? null : e.clientX; });
  box.addEventListener("pointerup", (e) => {
    if (x0 === null) return;
    const dx = e.clientX - x0;
    x0 = null;
    if (Math.abs(dx) > 50) go((dx < 0) === (dir !== "rtl") ? 1 : -1);   // a swipe toward the start of a line is "next"
  });
  document.body.append(box);
}

function show() {
  const src = list[at];
  box.classList.remove("zoom");
  const img = box.querySelector(".lb-img");
  img.src = src.src;
  img.alt = src.alt || "";
  box.querySelector(".lb-count").textContent = `${at + 1} / ${list.length}`;
  box.querySelector(".lb-name").textContent = src.name;
  box.querySelector(".lb-open").href = src.src;
  box.querySelectorAll(".lb-prev, .lb-next").forEach((b) => { b.hidden = list.length < 2; });
  box.querySelector(".lb-count").hidden = list.length < 2;
}

function go(d) {
  if (list.length < 2) return;
  at = (at + d + list.length) % list.length;
  show();
}

export function close() {
  if (!box || box.hidden) return false;
  box.hidden = true;
  box.querySelector(".lb-img").removeAttribute("src");
  document.documentElement.classList.remove("lb-open");
  last?.focus?.();
  return true;
}

let last = null;
function nameOf(src) {
  const p = decodeURIComponent((String(src).split("path=")[1] || String(src)).split("&")[0]);   // cut at the literal & (v=…) before decoding: a name may hold an encoded one
  return p.split("/").pop();
}

export function openLightbox(imgs, i) {
  if (!box) build();
  last = document.activeElement;
  list = imgs.map((im) => ({ src: im.currentSrc || im.src, alt: im.alt, name: nameOf(im.src) }));
  at = Math.max(0, i);
  show();
  box.hidden = false;
  document.documentElement.classList.add("lb-open");
  box.querySelector(".lb-close").focus({ preventScroll: true });
}

// a click on a feed image opens the lightbox with the images of the same message (not a new tab)
document.addEventListener("click", (e) => {
  const im = e.target.closest?.(PICK);
  if (!im || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  const scope = im.closest(".row") || im.closest(".feed");
  const imgs = [...scope.querySelectorAll(PICK.replaceAll(".feed ", ""))];
  openLightbox(imgs, imgs.indexOf(im));
}, true);

// keys — ahead of the others: Esc closes the image rather than stopping the turn or closing the file
window.addEventListener("keydown", (e) => {
  if (!box || box.hidden) return;
  if (e.key === "Escape") close();
  else if (e.key === "ArrowLeft") go(dir === "rtl" ? 1 : -1);   // the arrows point at the neighbors on the screen
  else if (e.key === "ArrowRight") go(dir === "rtl" ? -1 : 1);
  else return;
  e.preventDefault();
  e.stopImmediatePropagation();
}, true);
