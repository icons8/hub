// A core module's HTML view calls the endpoints of ITS OWN module (Module API 2.3). The view /mod/<name>/view/… lives in
// a sandboxed <iframe> (view CSP: opaque origin, the hub API is closed to it). When such a frame loads, the page gives
// it a port (MessageChannel, accepted by hubmod.VIEW_JS) and relays its requests through it only to /api/mod/<name>/…
// — the name comes from the frame's address. A port serves one load: if the view navigates elsewhere inside the frame
// on its own (link, location), there is no new port, so a foreign document in the frame gets no endpoints; a new src,
// a frame re-inserted into the page, and a navigation requested by the view ({go: path} to the port, hub.go — only
// within /mod/<name>/view/) get a port again.
// A request from the view carries X-Hub-View: 1 — the module does not get X-Hub-Owner: the owner's approval never
// comes from a view.
import { t } from "./i18n.js";

const VIEW = /^\/mod\/([a-z][a-z0-9-]{0,39})\/view(?:\/|$)/;
const METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);
const frames = new WeakMap();   // iframe -> {given — the port for this load has been handed out, port}

function moduleOf(frame) {
  let u;
  try { u = new URL(frame.getAttribute("src") || "", location.href); } catch { return null; }
  const m = u.origin === location.origin ? u.pathname.match(VIEW) : null;
  return m ? m[1] : null;
}

function fresh(rec) {   // the frame's next load is its own src again: it may get a port
  rec.given = false;
  if (rec.port) { rec.port.close(); rec.port = null; }
}

function watch(frame) {
  const known = frames.get(frame);
  if (known) return fresh(known);   // the frame was re-inserted — it loads its src again
  const rec = { given: false, port: null };
  frames.set(frame, rec);
  frame.addEventListener("load", () => {
    if (rec.given) return;   // a second load without a new src — the document left its own address
    rec.given = true;
    const name = moduleOf(frame);
    if (!name || !frame.contentWindow) return;
    const ch = new MessageChannel();
    rec.port = ch.port1;
    ch.port1.onmessage = (e) => (e.data && "go" in e.data ? go(frame, name, e.data.go) : relay(name, ch.port1, e.data));
    frame.contentWindow.postMessage({ hub: "port" }, "*", [ch.port2]);
  });
}

function go(frame, name, path) {   // navigation within the view's own tree: a new src means a new port
  const root = `/mod/${name}/view/`;
  let url;
  try { url = new URL(String(path).replace(/^[/\\]+/, ""), location.origin + root); } catch { return; }
  if (url.origin === location.origin && (url.pathname + "/").startsWith(root)) frame.src = url.pathname + url.search;
}

async function relay(name, port, msg) {
  const id = msg && msg.id;
  const fail = (error) => port.postMessage({ id, error });
  if (!msg || typeof msg.path !== "string") return fail(t("web.modview.path_string"));
  const method = String(msg.method || "GET").toUpperCase();
  if (!METHODS.has(method)) return fail(t("web.modview.no_method", { method, methods: [...METHODS].join(", ") }));
  if (msg.body !== undefined && msg.body !== null && typeof msg.body !== "string") return fail(t("web.modview.body_string"));
  const root = `/api/mod/${name}/`;
  let url;
  try { url = new URL(msg.path.replace(/^[/\\]+/, ""), location.origin + root); } catch { return fail(t("web.modview.bad_path")); }
  if (url.origin !== location.origin || !(url.pathname + "/").startsWith(root)) {
    return fail(t("web.modview.own_only", { name }));
  }
  const headers = { "X-Hub": "1", "X-Hub-View": "1" };
  if (typeof msg.type === "string" && !/[\r\n]/.test(msg.type)) headers["Content-Type"] = msg.type;
  try {
    const body = method === "GET" || method === "DELETE" ? undefined : (msg.body ?? "");
    const r = await fetch(url.pathname + url.search, { method, headers, body, cache: "no-store" });
    const type = r.headers.get("Content-Type") || "";
    if (type.startsWith("text/event-stream")) {
      r.body?.cancel();
      return fail(t("web.modview.no_stream"));
    }
    port.postMessage({ id, status: r.status, type, body: await r.text() });
  } catch (e) {
    fail(String((e && e.message) || e));
  }
}

export function bindModuleViews() {
  for (const f of document.querySelectorAll("iframe")) {   // embedded before the page loaded: reload it, now with a port
    watch(f);
    if (moduleOf(f)) f.setAttribute("src", f.getAttribute("src"));
  }
  new MutationObserver((recs) => {
    for (const r of recs) {
      if (r.type === "attributes") {
        if (r.target.tagName === "IFRAME") { const rec = frames.get(r.target); rec ? fresh(rec) : watch(r.target); }
        continue;
      }
      for (const n of r.addedNodes) {
        if (n.nodeType !== 1) continue;
        if (n.tagName === "IFRAME") watch(n);
        n.querySelectorAll?.("iframe").forEach(watch);
      }
    }
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
}
