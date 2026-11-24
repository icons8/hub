// Helper for the page's e2e tests: CDP steps on an already open headless Chrome tab. Steps are a JSON list:
//   {"go": "<url>", "feed": "compact"} — feed view (default "Detailed"; null — as in a fresh browser);
//   "look": null — the theme as in a fresh browser (Clean); without it a browser with no theme chosen gets Classic, the look
//   the tests were written for (hub.theme on the page, files.theme in "Files"; a theme the test put in "local" stays);
//   "brief": {"steps": false} — the "Brief" checkboxes (Settings tab); without it, the defaults
//   {"go": "<url>"}  {"wait": "<text>", "ms": 8000}  {"gone": "<text>"}  {"count": "<text>"}
//   {"status": "<text>"} — data-st of the badge in the row with this text (a tool step)
//   {"click": "<button or link text>"}  {"href": "<link text>"} — the address of the link with this text
//   {"local": {"key": "value"}} — into localStorage before the page loads (before go)
//   {"go": "<url>", "init": "<js>"} — JS code run before the page scripts on every load
//   {"errors": <ms>} — reload the page and collect its JS errors over that time
//   {"eval": "<expression>"} — the value of the expression on the page (JSON)   {"sleep": <ms>} — wait
//   {"until": "<expression>", "ms": 5000} — wait until the expression becomes true (on timeout — its last
//   value, no error: the test checks it); use instead of sleep when waiting for the page to act by itself
//   {"shot": "<file.png>", "width": 1300, "height": 900, "full": true, "theme": "dark"} — screenshot; "clip": [x, y, w, h],
//   "scale": 2 — only that rectangle (page coordinates), enlarged
//   {"wheel": -120, "times": 1, "every": 30} — a real mouse wheel over the feed (negative — up); "over": "held" — where
//   the mouse is currently held (after a drag with keep); "on": "<css>" — over this element (the session list, say)
//   {"key": "ArrowDown"} — a real key press into the focused element (Tab, Enter, Escape, " ", arrows, letters);
//   "mods": ["ctrl"|"meta"|"shift"|"alt"] — with these held, "text": true — a letter as typed   {"typed": "text"} — type text into the focused element
//   {"frames": 1} — addresses of the page's embedded frames (iframe); a frame blocked by CSP — chrome-error://…
//   {"drag": "<css from>", "to": "<css to>", "at": 0.25, "keep": true} — a real mouse drag: press,
//   move, release over fraction at of the target height; keep — do not release (snapshot mid-drag), {"drop": 1} releases;
//   between — a JS expression right after the press, before the move; "clicks": 2 — the press is the second of a double click
//   {"dropfiles": ["<path>", …], "on": "<css>"} — files and folders from disk dropped on an element (as from Finder)
//   {"hover": "<css>"} — move a real mouse over the element (no press) and leave it there
//   {"mclick": "<css>"} — a real left click in the middle of the element: it goes to whatever is on top there (what a person
//   hits); an element covered by another one at its middle is a step error ("hit": what covers it)
//   "clicks": 3 — a double or triple click there (each press with its click count, as a person clicks)
//   {"tap": "<css>"} — the same with a finger tap (touch stays on, as after "press")
//   {"rclick": "<css>"} — right click   {"press": "<css>", "ms": 650} — long finger press (touch)
//   {"touch": true} — the page as on a touch screen (pointer: coarse, hover: none); false — the mouse again
//   {"cpu": 6} — the page's CPU 6 times slower (as under machine load); 1 — normal, at the end of the steps — by itself
// Prints a JSON list of results; a step error — exit code 1.
const [port, stepsJson] = process.argv.slice(2);
const tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const ws = new WebSocket(tabs.find((t) => t.type === "page").webSocketDebuggerUrl);
let id = 0;
const wait = new Map();
const errs = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && wait.has(d.id)) { wait.get(d.id)(d); wait.delete(d.id); }
  else if (d.method === "Runtime.exceptionThrown") errs.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  else if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") errs.push(d.params.args.map((a) => a.value || a.description).join(" "));
};
await new Promise((r) => (ws.onopen = r));
const send = (method, params = {}) => new Promise((r) => { const i = ++id; wait.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
// headless Chrome puts a download attachment into ~/Downloads of whoever runs the tests — do not download
await send("Page.setDownloadBehavior", { behavior: "deny" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// an error of CDP itself (the document changed under the call: "back" loaded the page anew) is "no value yet", not a crash
const js = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true })).result?.result?.value;
// the system theme is light, as the tests expect: headless Chrome on Windows follows the machine's dark mode ({"shot",
// "theme"} still sets its own). The override ends with each run, so on a dark machine the page is dark here again: wait
// until the switch's transitions end (a button's border-color, 120 ms), or the first step reads a colour half-way
const dark = await js("matchMedia('(prefers-color-scheme: dark)').matches");
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
for (const end = Date.now() + 2000; dark && Date.now() < end && await js("document.getAnimations().some((a) => a instanceof CSSTransition)");) await sleep(50);
const text = async () => (await js("document.querySelector('main') ? document.querySelector('main').innerText : ''")) ?? "";
const out = [];
// a point on the element (fraction f of its height); show — first scroll it into view (if visible — do not move: a drag
// target found before scrolling will not go off the edge)
const spot = (sel, f = 0.5, show = false) => js(`(() => { const n = document.querySelector(${JSON.stringify(sel)}); if (!n) return null;
  ${show ? "n.scrollIntoView({ block: 'nearest' });" : ""} const r = n.getBoundingClientRect(); return [r.left + Math.min(40, r.width / 2), r.top + r.height * ${f}]; })()`);
const fail = (what) => { out.push({ ...what, ok: false }); console.log(JSON.stringify(out)); process.exit(1); };
let held = null;
for (const s of JSON.parse(stepsJson)) {
  if (s.sleep) {
    await sleep(s.sleep);
    out.push({ sleep: s.sleep });
  } else if (s.tz) {   // {"tz": "Etc/GMT+3"} — the page's time zone, whatever the machine's (a time the test knows)
    await send("Emulation.setTimezoneOverride", { timezoneId: s.tz });
    out.push({ tz: s.tz });
  } else if (s.eval) {
    out.push({ eval: await js(s.eval) });
  } else if (s.until) {
    const end = Date.now() + (s.ms || 5000);
    let v = await js(s.until);
    while (!v && Date.now() < end) { await sleep(100); v = await js(s.until); }
    out.push({ until: v });
  } else if (s.errors) {
    await send("Runtime.enable");
    await send("Page.reload");
    await sleep(s.errors);
    out.push({ errors: errs.slice(0, 5).map((e) => String(e).slice(0, 300)) });
  } else if (s.local) {
    await send("Page.addScriptToEvaluateOnNewDocument", { source: Object.entries(s.local).map(([k, v]) =>
      `try { localStorage.setItem(${JSON.stringify(k)}, ${JSON.stringify(v)}) } catch {}`).join(";") });
    out.push({ local: Object.keys(s.local) });
  } else if (s.shot) {
    await send("Emulation.setDeviceMetricsOverride", { width: s.width || 1300, height: s.height || 900, deviceScaleFactor: 1, mobile: (s.width || 1300) < 600 });
    if (s.theme) await send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: s.theme }],
    });
    await sleep(400);
    let clip;
    if (s.full) {
      const h = await js("Math.max(...[...document.querySelectorAll('main .feed, main')].map((n) => n.scrollHeight))");
      await send("Emulation.setDeviceMetricsOverride", { width: s.width || 1300, height: Math.min(h + 120, 6000), deviceScaleFactor: 1, mobile: false });
      await sleep(400);
    }
    const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: !!s.full,
      ...(s.clip ? { clip: { x: s.clip[0], y: s.clip[1], width: s.clip[2], height: s.clip[3], scale: s.scale || 1 } } : {}) });
    (await import("node:fs")).writeFileSync(s.shot, Buffer.from(r.result.data, "base64"));
    out.push({ shot: s.shot });
  } else if (s.go) {
    await send("Page.enable");
    // feed view — "Detailed" unless the step sets its own ("feed": "compact" | "minimal", null — as in a fresh browser)
    const feed = s.feed === undefined ? "localStorage.setItem('hub.feed', 'full')"
      : s.feed === null ? "localStorage.removeItem('hub.feed')" : `localStorage.setItem('hub.feed', ${JSON.stringify(s.feed)})`;
    // the "Brief" checkboxes (Settings tab) — from the step ("brief": {"steps": false}), otherwise the defaults
    const brief = s.brief ? `localStorage.setItem('hub.brief', ${JSON.stringify(JSON.stringify(s.brief))})` : "localStorage.removeItem('hub.brief')";
    const look = s.look === null ? "" : ["hub.theme", "files.theme"].map((k) =>
      `if (!localStorage.getItem('${k}')) localStorage.setItem('${k}', '{"id":"core:classic"}')`).join("; ");
    await send("Page.addScriptToEvaluateOnNewDocument", { source: `try { ${feed}; ${brief}; ${look}; ${s.init || ""} } catch {}` });
    await send("Page.navigate", { url: s.go });
    out.push({ go: s.go });
  } else if (s.key) {
    const codes = { Tab: 9, Enter: 13, Escape: 27, " ": 32, PageUp: 33, PageDown: 34, End: 35, Home: 36, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40 };
    const letter = /^[a-z]$/i.test(s.key);
    const code = s.key === " " ? "Space" : letter ? "Key" + s.key.toUpperCase() : s.key, vk = codes[s.key] ?? (letter ? s.key.toUpperCase().charCodeAt(0) : undefined);
    // "mods": ["ctrl", "shift", "meta", "alt"] — with the keys held (⌘F, Shift+Enter)
    const modifiers = (s.mods || []).reduce((m, k) => m | { alt: 1, ctrl: 2, meta: 4, shift: 8 }[k], 0);
    // "text": true — a letter as typed (the key event carries its character: it lands where the focus is after keydown)
    const extra = s.mods?.length ? {} : s.key === "Enter" ? { text: "\r" } : s.key === " " ? { text: " " } : s.text ? { text: s.key } : {};
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: s.key, code, windowsVirtualKeyCode: vk, modifiers, ...extra });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: s.key, code, windowsVirtualKeyCode: vk, modifiers });
    await sleep(80);
    out.push({ key: s.key });
  } else if (s.typed != null) {   // {"typed": "text"} — type into the focused element (as from the keyboard, an input event)
    // ms: from sending the key until the page has handled it — the time a key waits behind the page's long tasks is in it
    const sent = performance.now();
    await send("Input.insertText", { text: s.typed });
    const ms = performance.now() - sent;
    await sleep(50);
    out.push({ typed: s.typed, ms });
  } else if (s.wheel) {   // a real mouse wheel over the feed: {"wheel": -120} — up, {"wheel": -120, "times": 5} — several
    const at = s.over === "held" && held ? held   // the wheel where the mouse is held (mid-drag)
      : s.on ? await js(`(() => { const e = document.querySelector(${JSON.stringify(s.on)}); if (!e) return null; const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`)   // over this element
      : await js("(() => { const r = document.querySelector('main .feed').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()");
    if (!at) fail({ wheel: s.wheel, on: s.on });
    for (let i = 0; i < (s.times || 1); i++) {
      await send("Input.dispatchMouseEvent", { type: "mouseWheel", x: at[0], y: at[1], deltaX: 0, deltaY: s.wheel });
      await sleep(s.every || 30);
    }
    out.push({ wheel: s.wheel });
  } else if (s.hash) {
    await js(`location.hash = ${JSON.stringify(s.hash)}`);
    out.push({ hash: s.hash });
  } else if (s.wait || s.gone) {
    const t = s.wait || s.gone, end = Date.now() + (s.ms || 8000);
    let ok = false;
    while (Date.now() < end) { const has = (await text()).includes(t); if (s.wait ? has : !has) { ok = true; break; } await sleep(100); }
    out.push({ [s.wait ? "wait" : "gone"]: t, ok });
    if (!ok) { console.log(JSON.stringify(out)); console.error(await text()); process.exit(1); }
  } else if (s.count) {
    out.push({ count: s.count, n: (await text()).split(s.count).length - 1 });
  } else if (s.click) {
    const ok = await js(`(() => { for (const b of document.querySelectorAll('button, a')) if (b.innerText.trim().startsWith(${JSON.stringify(s.click)})) { b.click(); return true; } return false; })()`);
    out.push({ click: s.click, ok });
    if (!ok) { console.log(JSON.stringify(out)); process.exit(1); }
  } else if (s.href) {
    out.push({ href: s.href, url: await js(`(() => { for (const a of document.querySelectorAll('main a')) if (a.innerText.trim() === ${JSON.stringify(s.href)}) return a.href; return null; })()`) });
  } else if (s.manifest) {   // the app manifest as the browser parsed it: address, errors, parsed JSON
    const m = (await send("Page.getAppManifest")).result;
    out.push({ manifest: { url: m.url, errors: m.errors, data: m.data ? JSON.parse(m.data) : null } });
  } else if (s.frames) {   // addresses of the page's embedded frames (iframe); blocked by CSP — chrome-error://…
    const urls = [], walk = (n) => { for (const c of n.childFrames || []) { urls.push(c.frame.url); walk(c); } };
    walk((await send("Page.getFrameTree")).result.frameTree);
    out.push({ frames: urls });
  } else if (s.drag) {
    const a = await spot(s.drag, 0.5, true), b = await spot(s.to, s.at ?? 0.5);
    if (!a || !b) fail({ drag: s.drag, to: s.to });
    const n = s.clicks || 1, mouse = (type, x, y, buttons, c = n) => send("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons, clickCount: c });
    for (let c = 1; c < n; c++) { await mouse("mousePressed", a[0], a[1], 1, c); await mouse("mouseReleased", a[0], a[1], 0, c); }
    await mouse("mousePressed", a[0], a[1], 1);
    if (s.between) await js(s.between);
    for (let i = 1; i <= 10; i++) { await mouse("mouseMoved", a[0] + (b[0] - a[0]) * i / 10, a[1] + (b[1] - a[1]) * i / 10, 1); await sleep(25); }
    if (s.keep) held = b; else await mouse("mouseReleased", b[0], b[1], 0);
    await sleep(80);
    out.push({ drag: s.drag, to: s.to });
  } else if (s.hover) {
    const a = await spot(s.hover, 0.5, true);
    if (!a) fail({ hover: s.hover });
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: a[0], y: a[1], button: "none", buttons: 0 });
    await sleep(80);
    out.push({ hover: s.hover });
  } else if (s.drop) {
    if (held) await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: held[0], y: held[1], button: "left", buttons: 0, clickCount: 1 });
    held = null;
    await sleep(80);
    out.push({ drop: 1 });
  } else if (s.dropfiles) {
    const a = await spot(s.on, 0.5, true);
    if (!a) fail({ dropfiles: s.dropfiles, on: s.on });
    const data = { items: [], files: s.dropfiles, dragOperationsMask: 1 };
    for (const type of ["dragEnter", "dragOver", "drop"]) await send("Input.dispatchDragEvent", { type, x: a[0], y: a[1], data });
    await sleep(80);
    out.push({ dropfiles: s.dropfiles.length });
  } else if (s.mclick || s.tap) {
    const sel = s.mclick || s.tap;
    const a = await js(`(() => { const n = document.querySelector(${JSON.stringify(sel)}); if (!n) return null; n.scrollIntoView({ block: "nearest" });
      const r = n.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2, h = document.elementFromPoint(x, y);
      return [x, y, h ? h.tagName.toLowerCase() + "." + [...h.classList].join(".") : null, h === n || n.contains(h)]; })()`);
    if (!a || !a[3]) fail({ [s.mclick ? "mclick" : "tap"]: sel, hit: a && a[2] });   // not found, or covered by another element
    if (s.tap) {
      await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
      await send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: a[0], y: a[1] }] });
      await send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } else {
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: a[0], y: a[1], button: "none", buttons: 0 });
      for (let n = 1; n <= (s.clicks || 1); n++) {
        await send("Input.dispatchMouseEvent", { type: "mousePressed", x: a[0], y: a[1], button: "left", buttons: 1, clickCount: n });
        await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: a[0], y: a[1], button: "left", buttons: 0, clickCount: n });
      }
    }
    await sleep(120);
    out.push({ [s.mclick ? "mclick" : "tap"]: sel, hit: a[2], on: a[3] });
  } else if (s.rclick) {
    const a = await spot(s.rclick, 0.5, true);
    if (!a) fail({ rclick: s.rclick });
    for (const type of ["mousePressed", "mouseReleased"])
      await send("Input.dispatchMouseEvent", { type, x: a[0], y: a[1], button: "right", buttons: type === "mousePressed" ? 2 : 0, clickCount: 1 });
    await sleep(80);
    out.push({ rclick: s.rclick });
  } else if (s.press) {
    const a = await spot(s.press, 0.5, true);
    if (!a) fail({ press: s.press });
    await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
    await send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: a[0], y: a[1] }] });
    await sleep(s.ms || 650);
    await send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await sleep(80);
    out.push({ press: s.press });
  } else if (s.touch !== undefined) {
    await send("Emulation.setTouchEmulationEnabled", { enabled: !!s.touch, maxTouchPoints: 1 });
    out.push({ touch: !!s.touch });
  } else if (s.cpu) {
    await send("Emulation.setCPUThrottlingRate", { rate: s.cpu });
    out.push({ cpu: s.cpu });
  } else if (s.status) {
    out.push({ status: s.status, st: await js(`(() => { for (const r of document.querySelectorAll('main .row')) if (r.innerText.includes(${JSON.stringify(s.status)})) { const g = r.querySelector('[data-st]'); return g ? g.dataset.st : null; } return null; })()`) });
  }
}
await send("Emulation.setCPUThrottlingRate", { rate: 1 });   // the tab is shared between the test steps — do not leave the throttling on
console.log(JSON.stringify(out));
ws.close();
