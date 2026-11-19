// An input field like the session chat's, for plugins (API 1.7: a ready-made input component from the system, so
// plugins get a full input like the chat's): ❯ and a growing field, Enter sends, Shift+Enter adds a line, draft,
// paperclip, pasting an image with ⌘V and drag-and-drop (an [Image #N] marker in the text), voice — by button and by
// space in an empty field, an error that returns the text and files. It is now the same component as the session
// field (js/composer.js); the contract is unchanged. node is the form: the plugin puts it on the page and keeps it
// between snapshots (so the cursor and typed text stay).
import { buildComposer } from "./composer.js";
import { sessionName } from "./state.js";
import { t } from "./i18n.js";

const ENTER_HINT = t("web.inputbox.enter_hint");

// opts: to — recipient (session), key — draft key, placeholder, hint, prefix(text) — what to prepend to the text,
// wake() — whether to wake a sleeping session, onSent({text, to}) — after sending; send(body) — own transport (API 1.28):
// body is exactly what would go to /api/send (session, text, attachments, wake?, client_id), the reply is a hub.send
// receipt or {error} (text and files go back into the field, with that phrase; with a client_id in the reply, a retry
// of the same text goes out with it)
export function inputBox(opts) {
  const o = { hint: ENTER_HINT, ...opts };
  // the plugin's node for the bottom row (1.31): an exception or a non-element (text, fragment) leaves the field
  // without it, and the field keeps working
  const leading = () => {
    try {
      const node = o.leading();
      // the row is matched by element (children); nodeType, not instanceof: an element from an iframe counts too
      if (node == null || (typeof node === "object" && node.nodeType === 1)) return node || null;
      throw new TypeError("leading() returned " + typeof node);
    } catch (err) {
      if (o.fault) o.fault(err); else console.warn(err);
      return null;
    }
  };
  const props = () => ({
    key: o.key, to: o.to, label: o.label, placeholder: o.placeholder, hint: o.hint, cls: "inputbox",
    leading: typeof o.leading === "function" ? leading : null, leadingSig: o.leadingSig,
    send: typeof o.send === "function" ? (body) => o.send(body) : null,
    body: (words, files) => ({ session: o.to, text: o.prefix ? o.prefix(words) : words, attachments: files.map((p) => p.meta),
      ...(o.wake && o.wake() ? { wake: true } : {}) }),
    sent: ({ text, to }, flash) => {
      flash(t("web.inputbox.sent_to", { to: sessionName(to) }));   // for a few seconds the hint says where it went
      setTimeout(() => flash(""), 4000);
      if (o.onSent) o.onSent({ text, to });
    },
  });
  const c = buildComposer(props());
  return {
    node: c.node,
    focus: () => c.focus(),
    // start recording a voice message from the field as the microphone does, no click on the button needed (API 1.32, hub.capabilities.inputRecord):
    // call it from the plugin's own click handler. true — the microphone is being opened; false — voice is off, the browser cannot
    // record, the field is not on the page or another recording is going
    record: () => c.record(),
    // the field's draft (API 1.28): {text, files, uploading} — the plugin saves it and restores it itself (set)
    draft: () => c.draft(),
    // change the recipient, the hint in the field or the prefix on the fly (a thread opened an image — "about the screenshot…");
    // text and files (API 1.28) replace the draft: restore a saved one or clear the field
    set(next = {}) {
      const { text, files, ...rest } = next;
      Object.assign(o, rest);
      c.update(props());
      if (text !== undefined || files !== undefined) c.setDraft({ text, files });
    },
  };
}
