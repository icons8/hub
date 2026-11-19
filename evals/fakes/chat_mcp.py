#!/usr/bin/env python3
"""A fake chat MCP for the voice evals (stdio, JSON-RPC): `list-chat-messages` gives a scripted history, `send-chat-message`
sends nothing; both write the call to a log. CHAT_HISTORY — a JSON file with the messages (a list of {"from", "text",
"at"}), CHAT_LOG — where to append one JSON line per call: {"t": <time>, "tool": ..., "args": ...}. The start of the
server (`initialize`) is logged as the tool `_init`: a check "no call" must know the fake was up."""
import json
import os
import sys
import time

# a Windows console is not UTF-8: the history is Cyrillic and goes both ways
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

TOOLS = [
    {"name": "list-chat-messages", "description": "List the latest messages of a chat (Teams or Telegram dialog).",
     "inputSchema": {"type": "object", "properties": {"chatId": {"type": "string"}},
                     "required": ["chatId"]}},
    {"name": "send-chat-message", "description": "Send a message to a chat as the owner.",
     "inputSchema": {"type": "object", "properties": {"chatId": {"type": "string"}, "text": {"type": "string"}},
                     "required": ["chatId", "text"]}},
]


def log(tool, args):
    with open(os.environ["CHAT_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "tool": tool, "args": args}, ensure_ascii=False) + "\n")


def reply(mid, result):
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}, ensure_ascii=False), flush=True)


def text(s):
    return {"content": [{"type": "text", "text": s}]}


for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    method, mid = m.get("method"), m.get("id")
    if method == "initialize":
        log("_init", {})
        reply(mid, {"protocolVersion": (m.get("params") or {}).get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "chat", "version": "0"}})
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        p = m.get("params") or {}
        log(p.get("name"), p.get("arguments") or {})
        if p.get("name") == "list-chat-messages":
            with open(os.environ["CHAT_HISTORY"], encoding="utf-8") as f:
                reply(mid, text(json.dumps({"value": json.load(f)}, ensure_ascii=False)))
        else:
            reply(mid, text("sent"))
    elif mid is not None:
        reply(mid, {})
