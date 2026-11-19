// Page extension example (EXTENDING.md): a "Hello" item in the sidebar and a view of its own.
export default function register(hub) {
  hub.addView({
    id: "hello",
    title: "Hello",
    subtitle: "an extension example",
    render(root, snap) {
      const busy = snap.sessions.filter((s) => s.state === "busy").map((s) => s.name);
      const box = hub.el("div");
      box.append(hub.el("h2", null, "Hello from the extension"),
        hub.el("p", null, `Sessions: ${snap.sessions.length}, busy now: ${busy.join(", ") || "none"}.`));
      const btn = hub.el("button", "btn", "Say hello to the hub");
      btn.addEventListener("click", () => hub.post("/api/send", { session: snap.sessions.find((s) => s.kind === "hub")?.name, text: "hello from the extension", action: true }));
      box.append(btn);
      root.replaceChildren(box);
    },
  });
}
