// Page extension example (EXTENDING.md): a "Hello" item in the sidebar and a view of its own.
export default function register(hub) {
  let query = "", shown = null;
  const paint = (root, snap) => {
    const busy = snap.sessions.filter((s) => s.state === "busy").map((s) => s.name);
    const found = snap.sessions.filter((s) => s.name.toLowerCase().includes(query));
    const box = hub.el("div");
    box.append(hub.el("h2", null, "Hello from the extension"),
      hub.el("p", null, `Sessions: ${snap.sessions.length}, busy now: ${busy.join(", ") || "none"}.`),
      hub.el("ul", "hello-found"));
    box.lastChild.append(...found.map((s) => hub.el("li", null, s.name)));
    const btn = hub.el("button", "btn", "Say hello to the hub");
    btn.addEventListener("click", () => hub.post("/api/send", { session: snap.sessions.find((s) => s.kind === "hub")?.name, text: "hello from the extension", action: true }));
    box.append(btn);
    root.replaceChildren(box);
  };
  hub.addView({
    id: "hello",
    title: "Hello",
    subtitle: "an extension example",
    // API 1.39: while the view is open, the page's search field finds its sessions (older hosts ignore it)
    search: { placeholder: "Find a session here", query(text) { query = text.trim().toLowerCase(); if (shown) paint(shown, hub.snapshot()); } },
    render(root, snap) { shown = root; paint(root, snap); },
  });
}
