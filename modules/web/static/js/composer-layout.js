// Optional CSS layout contract for the shared composer. Themes opt in with
// --composer-wrap: 1 and style .composer-stacked; 0 keeps responsive observation
// while disabling stacking. An absent property leaves the default core untouched.
// The owner calls measure from its normal grow path; no DOM discovery or mutation observer.
let probe;
const number = value => parseFloat(value) || 0;
function textHeight(text, style, width) {
  if (!probe?.isConnected) {
    probe = document.createElement("textarea");
    probe.tabIndex = -1;
    probe.setAttribute("aria-hidden", "true");
    // Reduced-motion CSS may assign a tiny duration to every element. That still
    // defers width changes, so disable the transition property on this probe.
    probe.style.cssText = "position:fixed;left:-10000px;top:0;visibility:hidden;pointer-events:none;box-sizing:border-box;height:0;min-height:0;max-height:none;margin:0;border:0;resize:none;overflow:hidden;transition-property:none!important;animation:none!important;";
    document.body.append(probe);
  }
  for (const key of ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight", "letterSpacing", "wordSpacing",
    "textTransform", "textIndent", "whiteSpace", "wordBreak", "overflowWrap", "tabSize",
    "paddingTop", "paddingBottom", "paddingLeft", "paddingRight"]) probe.style[key] = style[key];
  probe.style.width = Math.max(1, width) + "px";
  probe.value = text || " ";
  return probe.scrollHeight;
}

export function composerLayout(form, textarea, attach, bar) {
  let latched = false, observer = null, observed = false, width = 0, frame = 0, opted = null, check = 0;
  const unwatch = () => { observer?.disconnect(); observed = false; cancelAnimationFrame(frame); frame = 0; };
  const modeOf = (fs) => fs.getPropertyValue("--composer-wrap").trim();
  // no opt-in: the owner draws on every snapshot right after the feed is rebuilt, and reading the property there made the
  // browser compute the styles of the whole page mid-redraw. The answer is kept and read again after the frame, when the
  // styles are computed and the read costs nothing; an opt-in found then grows the field
  const recheck = (grow) => {
    if (check) return;
    check = requestAnimationFrame(() => setTimeout(() => {
      check = 0;
      if (opted !== false || !form.isConnected) return;
      const mode = modeOf(getComputedStyle(form));
      if (mode === "1" || mode === "0") { opted = null; grow(); }
    }));
  };
  return {
    // null asks the core to use its existing growth. Otherwise return the measured
    // textarea height; core still owns the node, draft, selection and send handlers.
    measure(grow) {
      if (!textarea.value) latched = false;
      if (!form.isConnected) { unwatch(); opted = null; return null; }
      if (opted === false) { recheck(grow); return null; }
      const fs = getComputedStyle(form);
      const mode = modeOf(fs);
      opted = mode === "1" || mode === "0";
      if (!opted) {
        form.classList.toggle("composer-stacked", false); latched = false; unwatch(); return null;
      }
      if (!observed && typeof ResizeObserver === "function") {
        observer ||= new ResizeObserver(() => {
          if (!form.isConnected) return unwatch();
          const next = form.getBoundingClientRect().width;
          if (next === width) return;
          width = next;
          cancelAnimationFrame(frame);
          frame = requestAnimationFrame(() => { frame = 0; grow(); });
        });
        width = form.getBoundingClientRect().width;
        observer.observe(form); observed = true;
      }
      if (mode === "0" || form.matches(".rec,.has-leading")) { form.classList.toggle("composer-stacked", false); latched = false; return null; }
      if (!form.getClientRects().length || !form.clientWidth) return null;
      const style = getComputedStyle(textarea);
      const full = form.getBoundingClientRect().width - number(fs.borderLeftWidth) - number(fs.borderRightWidth)
        - number(fs.paddingLeft) - number(fs.paddingRight) - number(fs.getPropertyValue("--composer-wrap-right"));
      const narrow = full - attach.getBoundingClientRect().width - bar.getBoundingClientRect().width
        - number(fs.getPropertyValue("--composer-wrap-gap"));
      if (textarea.value && !latched) {
        const single = textHeight(" ", style, narrow);
        latched = /[\r\n\u2028\u2029]/.test(textarea.value) || textHeight(textarea.value, style, narrow) > single + 0.5;
      }
      form.classList.toggle("composer-stacked", latched);
      return Math.min(textHeight(textarea.value, style, latched ? full : narrow), innerHeight * 0.4);
    },
  };
}
