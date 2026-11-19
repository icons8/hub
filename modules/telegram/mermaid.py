"""Mermaid diagrams from session replies, sent to Telegram as a picture. The web page draws them itself; Telegram would
show the source. Headless Chrome draws the diagram with the same mermaid as the page (modules/web/vendor) into a PNG of
its size: the first pass finds out the size, the second takes the screenshot."""
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import time

from core import redact

BLOCK = re.compile(r"```mermaid[^\n]*\n(.*?)```", re.S)
MERMAID = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "vendor", "mermaid.min.js")
# the diagram text is foreign (a session may quote anything): the page may run only its own two scripts (nonce), with no
# network and no plugins; the text goes into the script as a JSON string with <, > and & escaped, so it cannot close the
# script or open a comment
PAGE = """<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; \
script-src 'nonce-%(nonce)s'; style-src 'unsafe-inline'; img-src data:; font-src data:"><title>…</title>
<style>html,body{margin:0;background:#fff}#c{display:inline-block;padding:16px}</style>
<div id="c"></div><script nonce="%(nonce)s" src="%(lib)s"></script><script nonce="%(nonce)s">
mermaid.initialize({startOnLoad: false, securityLevel: "strict", theme: "default"});
mermaid.render("m", %(code)s).then((r) => {
  document.getElementById("c").innerHTML = r.svg;
  const b = document.getElementById("c").getBoundingClientRect();
  document.title = Math.ceil(b.width) + "x" + Math.ceil(b.height);
}).catch(() => { document.title = "error"; });
</script>"""


def split(md):
    """Text and diagrams: [(start, end, code)] of the ```mermaid blocks."""
    return [(m.start(), m.end(), m.group(1).strip()) for m in BLOCK.finditer(md)]


def page(code, nonce):
    """The page that renders one diagram."""
    literal = json.dumps(code).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return PAGE % {"nonce": nonce, "lib": pathlib.Path(MERMAID).as_uri(), "code": literal}


def args(chrome, extra):
    """Chrome command line: as root (Linux, Docker) it does not start without --no-sandbox. The local page gets no
    access to other local files (hub shot/video snapshots need it — modules/proof asks for it itself)."""
    root = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
    return [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--hide-scrollbars", *root, *extra]


def stop(proc):
    """Stop our own Chrome with its helpers: on macOS and Linux its process group, on Windows there is no group, so the process itself."""
    import platforms
    if not platforms.real_pid(proc.pid):   # not a real child: killpg would hit other processes
        return
    if proc.returncode is not None and platforms.current.alive(proc.pid):   # collected, and the number is someone else's
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL) if hasattr(os, "killpg") else proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass


def _without_keys():
    """Chrome's environment: Chrome needs none of the hub's keys (redact.child_env). A process of the previous release
    that loads this file anew keeps its own core.redact in memory: then the environment as before."""
    return redact.child_env(os.environ) if hasattr(redact, "child_env") else dict(os.environ)


def _chrome(chrome, extra, timeout, done, out=None):
    """Run Chrome until the result is ready (done() is true), then stop it: headless Chrome gives the DOM and a screenshot within a second,
    but does not always exit by itself (hung until the timeout once). The output goes to a file, not a pipe."""
    group = {"creationflags": 0x00000200} if os.name == "nt" else {"start_new_session": True}
    with open(out or os.devnull, "w") as f:
        proc = subprocess.Popen(args(chrome, extra), stdout=f, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                env=_without_keys(), **group)
        end = time.time() + timeout
        try:
            while time.time() < end and not done() and proc.poll() is None:
                time.sleep(0.2)
            time.sleep(0.3)   # let the file finish being written
            return done()
        finally:
            stop(proc)
            from modules.testchrome import litter   # its com.google.Chrome.* directory is outside the profile, in the system folder
            for a in extra:
                if a.startswith("--user-data-dir="):
                    litter.own(a.split("=", 1)[1])


def render(code, chrome=None, timeout=60):
    """The diagram PNG (a path in a temp folder — removed by whoever sent it: shutil.rmtree(dirname)) or None."""
    if chrome is None:
        from modules.testchrome import chrome as found
        chrome = found.find()
    if not chrome or not os.path.exists(MERMAID):
        return None
    d = tempfile.mkdtemp(prefix="hub-mermaid-")
    html = os.path.join(d, "schema.html")
    with open(html, "w", encoding="utf-8") as f:
        f.write(page(code, secrets.token_urlsafe(16)))
    dom, png, url = os.path.join(d, "dom.html"), os.path.join(d, "schema.png"), pathlib.Path(html).as_uri()
    title = lambda: os.path.exists(dom) and re.search(r"<title>(\d+x\d+|error)</title>",  # noqa: E731
                                                      open(dom, encoding="utf-8", errors="replace").read())
    try:
        # 1) the diagram size: the page writes it into the title once mermaid has drawn; 2) a screenshot of exactly that size
        if _chrome(chrome, [f"--user-data-dir={d}/p1", "--window-size=3000,3000", "--virtual-time-budget=5000",
                            "--dump-dom", url], timeout, title, out=dom) and title().group(1) != "error":
            w, h = (int(x) for x in title().group(1).split("x"))
            w, h = min(w, 3000), min(h, 6000)
            if _chrome(chrome, [f"--user-data-dir={d}/p2", f"--window-size={w},{h}", "--virtual-time-budget=5000",
                                "--force-device-scale-factor=2", f"--screenshot={png}", url], timeout,
                       lambda: os.path.exists(png) and os.path.getsize(png) > 0):
                return png
    except Exception:   # any render failure — the diagram source goes as text, Telegram must not crash
        pass
    shutil.rmtree(d, ignore_errors=True)
    return None
