// The "To the latest" button under a feed: one node per feed kind (the session feed, a linked hub's feed), kept between
// redraws — snapshots rebuild the feed every second, and a press must not lose the node between mousedown and mouseup.
// On the right is the number of messages the owner has not seen yet. The caller decides when it is shown and where the
// press leads (onJump); this file is the button itself: the content, the counter, the label for screen readers, the place
// right after the feed (the Tab order is as on the screen), and the way out.
import { el, icon } from "./dom.js";
import { t } from "./i18n.js";

const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

export function jumpDown(onJump) {
  const node = el("button", "btn jumpnew");
  node.type = "button";
  let shown = -1;
  node.addEventListener("click", (e) => {
    // from the keyboard (with a mouse or a finger we do not move focus: on a phone it would raise the keyboard)
    const keyed = e.detail === 0 && document.activeElement === node;
    onJump();
    if (keyed) document.querySelector(".dock textarea")?.focus({ preventScroll: true });   // the button goes away — and the focus with it
  });
  return {
    node,
    // n — how many are new (n > 0 shows the button); main — the grid that holds the feed
    show(n, main) {
      if (!node.firstChild) {
        node.append(icon("down"), el("span", "l", t("web.main.jump_latest")), el("span", "n"), el("span", "sr"));
        node.lastChild.setAttribute("aria-live", "polite");
      }
      if (n !== shown) {   // the number changed: the circle, the label for screen readers and, once, the announcement "New: N"
        shown = n;
        const [, , cnt, live] = node.children;
        cnt.textContent = n || "";
        cnt.hidden = !n;
        live.textContent = n ? t("web.main.jump_live", { n }) : "";
        node.setAttribute("aria-label", n ? t("web.main.jump_aria", { n }) : t("web.main.jump_latest"));
      }
      node.classList.remove("leaving");
      const feedEl = main.querySelector(":scope > .feed");   // right after the feed, above the input
      if (feedEl && node.previousElementSibling !== feedEl) feedEl.after(node);
      else if (!feedEl && node.parentNode !== main) main.append(node);
    },
    hide() {
      if (!node.parentNode || node.classList.contains("leaving")) return;
      if (reduced()) node.remove();
      else {   // goes away fading; if it came back earlier the class is removed in show()
        node.classList.add("leaving");
        setTimeout(() => { if (node.classList.contains("leaving")) { node.classList.remove("leaving"); node.remove(); } }, 180);
      }
    },
  };
}
