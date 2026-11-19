// Input boxes on the page: the session box (#msg) and plugin boxes (hub.input, API 1.7). Attachments and voice find
// the box by its draft key and ask for it to be redrawn here — a plugin box lives its own life, outside the page's render().
export const boxes = new Map();   // draft key -> {ta, refresh}

export function refreshBoxes() {
  for (const b of boxes.values()) if (b.ta.isConnected) b.refresh();   // hidden (panel closed) — when it is shown
}
