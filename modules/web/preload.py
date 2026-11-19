"""Early hints for the page: the modules the first screen needs and the page strings are announced in <head>.

Without them the browser learns about each module only after it has run its parent (~5 levels of imports) and asks for
/i18n.json only when i18n.js runs; the stream and the first screen wait for that chain. The list is derived from the
files themselves (static `import ... from "./x.js"`), so a new module needs no registration; a file that is missing
or unreadable is just left out (the page works without a hint)."""
import os
import re
import threading

# static imports only: `import(...)` (loaded on demand) is not matched, it is not needed for the first screen
_IMPORT = re.compile(r"""(?:\bfrom|\bimport)\s*["'](\.{1,2}/[^"']+\.js)["']""")
_SCRIPT = re.compile(r"""<script\s+type="module"\s+src="(/static/[^"?]+\.js)"\s*>""")

_lock = threading.Lock()
_parsed = {}   # path -> ((mtime_ns, size), [paths it imports]); a changed file is read again


def _imports(path):
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
        with _lock:
            known = _parsed.get(path)
        if known and known[0] == sig:
            return known[1]
        with open(path, encoding="utf-8") as f:
            found = [os.path.normpath(os.path.join(os.path.dirname(path), m)) for m in _IMPORT.findall(f.read())]
    except (OSError, ValueError):
        return []
    with _lock:
        _parsed[path] = (sig, found)
    return found


def module_urls(static, html):
    """URLs (/static/...) of the module graph of the page's entry scripts, in breadth-first order (the entry first)."""
    root = os.path.realpath(static)
    # normalised as the imports are: one module is one key in `seen` (on Windows join keeps the "/" of the address)
    queue = [os.path.normpath(os.path.join(root, u[len("/static/"):])) for u in _SCRIPT.findall(html)]
    seen, urls = set(), []
    while queue:
        path = queue.pop(0)
        if path in seen or not os.path.realpath(path).startswith(root + os.sep):
            continue
        seen.add(path)
        if not os.path.isfile(path):
            continue
        urls.append("/static/" + os.path.relpath(path, root).replace(os.sep, "/"))
        queue.extend(_imports(path))
    return urls


def head_hints(static, html, prefix="/static/"):
    """The <link> tags to put into <head>: the strings of the page and its module graph, at the addresses under prefix
    ("/static/", or the versioned "/static/v/<ver>/" that the page itself uses, so the hint and the import are one
    request); "" when there is no module."""
    urls = module_urls(static, html)
    if not urls:
        return ""
    tags = ['<link rel="preload" href="/i18n.json" as="fetch" crossorigin>']   # the fetch in i18n.js: same URL, same credentials
    tags += [f'<link rel="modulepreload" href="{prefix}{u[len("/static/"):]}">' for u in urls]
    return "\n".join(tags) + "\n"
