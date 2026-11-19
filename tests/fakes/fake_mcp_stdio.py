#!/usr/bin/env python3
"""Fake chrome-devtools-mcp over stdio: answers every message with what it was (id, method, tool) and whether the
browser was up at that moment. FAKE_MCP_LOG — file to write every received message to. FAKE_MCP_SLOW_NEW_PAGE — how
many seconds to wait before answering new_page without delaying the reading of the following messages (the real one
answers calls interleaved); the moment of the answer is written to the log as the line {"answered": "new_page"}.
It keeps a list of pages like chrome-devtools-mcp (page 1 is someone else's; new_page adds one and selects it, close_page
removes) and mirrors them as tabs of the fake Chrome.
FAKE_MCP_NEW_PAGE_DELAYS — "1.5,0": the delay for each new_page in turn (the last one repeats), as FAKE_MCP_SLOW_NEW_PAGE.
FAKE_MCP_PROCS — a file each started process appends its pid to; its log is then FAKE_MCP_LOG.<number of the process>.
FAKE_MCP_STUCK — "<n>:silent|error" (n — a number or "all"): the first n processes hang like chrome-devtools-mcp whose
connection to the browser hung: no answer to tools/call, or an answer with its timeout error; "network": every tools/call
gets the error of a page whose Network.enable timed out (puppeteer keeps that failure, so it comes back at once), the
first one after FAKE_MCP_STUCK_FIRST seconds; FAKE_MCP_STUCK_AFTER: how many tools/call of such a process go well before
(its own tab opened before the page broke). FAKE_MCP_TIMEOUT_ONCE: seconds after which the first tools/call of a process
gets that error, the later ones go well (a page that was slow once). FAKE_MCP_SLOW_FIRST — seconds
the first process (of FAKE_MCP_PROCS) takes to answer its first new_page. FAKE_MCP_OK_TEXT — the text of a plain reply."""
import json
import os
import sys
import threading
import time
import urllib.request

lock = threading.Lock()


def log(line):
    if os.environ.get("FAKE_MCP_LOG"):
        with lock, open(os.environ["FAKE_MCP_LOG"], "a", encoding="utf-8") as f:
            f.write(line)


def answer(out, delay=0.0):
    time.sleep(delay)
    with lock:
        print(json.dumps(out), flush=True)
    if delay:
        log(json.dumps({"answered": "new_page", "id": out.get("id")}) + "\n")


stuck = None
if os.environ.get("FAKE_MCP_PROCS"):
    with lock, open(os.environ["FAKE_MCP_PROCS"], "a+") as f:
        f.seek(0)
        index = len(f.read().split())
        f.write(f"{os.getpid()}\n")
    os.environ["FAKE_MCP_LOG"] = f"{os.environ.get('FAKE_MCP_LOG')}.{index}"
    n, _, mode = (os.environ.get("FAKE_MCP_STUCK") or "0:").partition(":")
    if n == "all" or index < int(n):
        stuck = mode
delays = [float(x) for x in (os.environ.get("FAKE_MCP_NEW_PAGE_DELAYS") or "").split(",") if x]
if os.environ.get("FAKE_MCP_SLOW_FIRST") and stuck is None and os.environ.get("FAKE_MCP_PROCS") and index == 0:
    delays = [float(os.environ["FAKE_MCP_SLOW_FIRST"]), 0.0]   # only the first process is slow to open a page, once


timeout_once = os.environ.get("FAKE_MCP_TIMEOUT_ONCE")
stuck_first = float(os.environ.get("FAKE_MCP_STUCK_FIRST") or 0)
stuck_after = int(os.environ.get("FAKE_MCP_STUCK_AFTER") or 0)
TIMED_OUT = ("Network.enable timed out. Increase the 'protocolTimeout' setting in launch/connect calls for a higher "
             "timeout if needed.")


url = sys.argv[sys.argv.index("--browserUrl") + 1]
pages = {1: {"url": "http://127.0.0.1:8787/"}}   # page number -> {url, tab}
selected = 1


def chrome(path, method="GET"):
    try:
        req = urllib.request.Request(url + path, method=method)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=1) as r:
            return json.load(r) if path.startswith("/json/new") else None
    except (OSError, ValueError):
        return None


def listing():
    return "## Pages\n" + "\n".join(f"{n}: {p['url']}" + (" [selected]" if n == selected else "") for n, p in sorted(pages.items()))


for line in sys.stdin:
    msg = json.loads(line)
    log(line)
    try:
        # the real browser MCP (Node) does not apply the environment proxy to CDP — neither does the fake
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + "/json/version", timeout=1) as r:
            up = bool(json.load(r).get("Browser"))
    except OSError:
        up = False
    out = {"method": msg.get("method"), "up": up}
    if "id" not in msg:   # a notification: MCP does not answer it
        continue
    out["id"] = msg["id"]
    if stuck and msg.get("method") == "tools/call" and stuck_after:
        stuck_after -= 1
    elif stuck and msg.get("method") == "tools/call":
        if stuck == "error":
            answer({**out, "result": {"isError": True, "content": [{"type": "text", "text": (
                f'Tool "{(msg.get("params") or {}).get("name")}" timed out after 60000ms waiting for the browser '
                "connection to be established. The connection attempt will be retried automatically on the next "
                "tool call.")}]}})
        if stuck == "network":
            answer({**out, "result": {"isError": True, "content": [{"type": "text", "text": TIMED_OUT}]}}, stuck_first)
            stuck_first = 0.0
        continue
    if timeout_once is not None and msg.get("method") == "tools/call":
        answer({**out, "result": {"isError": True, "content": [{"type": "text", "text": TIMED_OUT}]}}, float(timeout_once))
        timeout_once = None
        continue
    args = (msg.get("params") or {}).get("arguments") or {}
    if (msg.get("params") or {}).get("name") == "close_page":
        gone = pages.pop(args.get("pageId"), None)
        if gone and gone.get("tab"):
            chrome("/json/close/" + gone["tab"])
        out["result"] = {"content": [{"type": "text", "text": listing()}]}
    if (msg.get("params") or {}).get("name") == "navigate_page":   # the selected page gets the url
        pages[selected]["url"] = args.get("url") or pages[selected]["url"]
    if (msg.get("params") or {}).get("name") == "new_page":   # like chrome-devtools-mcp: a list of pages, the new one selected
        # the browser did not open a tab (FAKE_MCP_NEW_PAGE_FAILS, or a url "bad:…"): an error instead of the list of pages
        out["result"] = {"isError": True, "content": [{"type": "text", "text": "Protocol error: Target.createTarget"}]}
        if not os.environ.get("FAKE_MCP_NEW_PAGE_FAILS") and not (args.get("url") or "").startswith("bad:"):
            number = max(pages) + 1
            tab = chrome("/json/new?" + (args.get("url") or "about:blank"), "PUT")
            pages[number] = {"url": args.get("url") or "about:blank", "tab": (tab or {}).get("id")}
            selected = number
            out["result"] = {"content": [{"type": "text", "text": listing()}]}
        slow = float(os.environ.get("FAKE_MCP_SLOW_NEW_PAGE") or 0)
        if delays:
            slow = delays[0] if len(delays) == 1 else delays.pop(0)
        if slow:
            threading.Thread(target=answer, args=(out, slow)).start()
            continue
    out.setdefault("result", {"content": [{"type": "text", "text": os.environ.get("FAKE_MCP_OK_TEXT") or "ok"}]})   # a JSON-RPC reply always has one
    answer(out)
