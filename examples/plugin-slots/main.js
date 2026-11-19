// Page plugin example (API v1, EXTENDING.md): a small element in every slot and a tab in Settings.
// render(root, snap, ctx) is called on every core snapshot with the same root; for session slots ctx.session is its row.
export default function register(hub) {
  const busy = (snap) => snap.sessions.filter((s) => ["busy", "starting"].includes(s.state)).length;
  hub.addSlot("topbar", { id: "busy", render(root, snap) { root.textContent = `busy: ${busy(snap)}`; } });
  hub.addSlot("sidebar.top", { id: "hint", render(root) { root.textContent = "🧩 sidebar.top"; } });
  hub.addSlot("sidebar.nav", { id: "nav", render(root) { root.textContent = "🧩 sidebar.nav"; } });
  hub.addSlot("sidebar.header", { id: "head", render(root) { root.textContent = "🧩"; root.title = "sidebar.header"; } });
  hub.addSlot("sidebar.footer", { id: "foot", render(root) { root.textContent = "🧩 sidebar.footer"; } });
  hub.addSlot("sidebar.session", { id: "dot", render(root, snap, { session }) {
    root.textContent = (session.background || []).length ? "⚙" : "";
    root.title = "background tasks: " + (session.background || []).length;
  } });
  hub.addSlot("session.header", { id: "tools", render(root, snap, { session }) {
    root.textContent = `🧩 tools: ${(session.tools || []).length}`;
  } });
  hub.addSlot("session.composer", { id: "tip", render(root, snap, { session }) {
    root.textContent = `🧩 session.composer · ${session.name}`;
  } });
  hub.addSettings({ id: "slots", title: "Example", hint: "The tab of the \"slots\" plugin", render(root) {
    if (!root.firstChild) root.append(hub.el("p", null, `Hub plugin API: ${hub.api}`));
  } });
}
