"""Its own headless Chrome and a conversation with it over CDP, no dependencies: a WebSocket client on a stdlib socket (only what
CDP needs: text frames, fragments, ping). The Chrome is the same one that draws mermaid diagrams (modules/testchrome: the setting
or the system one), with its own temporary profile; it is killed as a whole process group (06.10: headless Chrome does not always exit
by itself)."""
import atexit
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse

from core import i18n, redact
from modules.telegram import mermaid
from modules.testchrome import litter


class Error(Exception):
    pass


def _without_keys():
    """Chrome's environment: Chrome needs none of the hub's keys (redact.child_env). A process of the previous release
    that loads this file anew keeps its own core.redact in memory: then the environment as before."""
    return redact.child_env(os.environ) if hasattr(redact, "child_env") else dict(os.environ)


def frame(payload, opcode=1):
    """A client frame: FIN, opcode, mask (a client must mask), length of 7 / 16 / 64 bits."""
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def unmask(data, mask):
    return bytes(b ^ mask[i % 4] for i, b in enumerate(data))


class Socket:
    """A WebSocket to ws://127.0.0.1:<port>/<path>."""

    def __init__(self, url, timeout=30):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Error(i18n.t("cli.proof.chrome_closed_early"))
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise Error(i18n.t("cli.proof.chrome_ws_refused", head=head.splitlines()[0].decode(errors="replace")))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise Error(i18n.t("cli.proof.chrome_closed"))
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        self.sock.sendall(frame(text.encode()))

    def recv(self):
        """The next whole message (joining fragments); answers ping with pong."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = unmask(data, mask)
            if opcode == 9:
                self.sock.sendall(frame(data, 10))
                continue
            if opcode == 8:
                raise Error(i18n.t("cli.proof.chrome_closed"))
            if opcode in (0, 1, 2):
                parts.append(data)
                if b0 & 0x80:
                    return b"".join(parts).decode("utf-8", errors="replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Page:
    """One tab of its own headless Chrome: call() waits for the reply, events on the way go to on_event (video frames)."""

    def __init__(self, chrome=None, timeout=60, extra=()):
        from modules.testchrome import chrome as found
        if chrome is None:
            chrome = found.find()
        if not chrome:
            # macOS never takes the system Google Chrome (find(): a dock icon), so "install Google Chrome" would mislead there
            raise Error(i18n.t("cli.proof.no_chrome_mac", command=found.install_command()) if found.MAC
                        else i18n.t("cli.proof.no_chrome"))
        self.timeout, self.on_event, self.n = timeout, None, 0
        # Chrome lives in its own process group and does not outlive its parent by itself: a script that crashed before close() or was killed
        # left it with helpers for hours (12.10: dozens of chrome-headless-shell). The owner's pid mark in the profile means
        # the next Page will kill such a Chrome (litter.stop_proof_chromes); close() also runs on exit with an error
        litter.stop_proof_chromes(tempfile.gettempdir())
        self.dir = tempfile.mkdtemp(prefix="hub-proof-")
        with open(os.path.join(self.dir, "pid"), "w") as f:
            f.write(str(os.getpid()))
        atexit.register(self.close)
        group = {"creationflags": 0x00000200} if os.name == "nt" else {"start_new_session": True}
        self.proc = subprocess.Popen(mermaid.args(chrome, ["--allow-file-access-from-files",   # shots, as before
                                                           f"--user-data-dir={self.dir}", "--remote-debugging-port=0",
                                                           "--window-size=1440,900", *extra, "about:blank"]),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                     env=_without_keys(), **group)
        self.ws = None
        try:
            self.ws = Socket(self._endpoint(), timeout)
            target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        except BaseException:
            self.close()
            raise

    def _endpoint(self):
        """Port and path from DevToolsActivePort: Chrome writes it into the profile when ready (port 0 means any free one)."""
        path, end = os.path.join(self.dir, "DevToolsActivePort"), time.time() + self.timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise Error(i18n.t("cli.proof.chrome_not_started"))
            try:
                with open(path) as f:
                    port, ws = f.read().split()[:2]
                return f"ws://127.0.0.1:{port}{ws}"
            except (OSError, ValueError):
                time.sleep(0.1)
        raise Error(i18n.t("cli.proof.chrome_no_answer"))

    def call(self, method, params=None, page=True):
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if page and getattr(self, "session", None):
            msg["sessionId"] = self.session
        self.ws.send(json.dumps(msg))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                if "error" in m:
                    raise Error(f"{method}: {m['error'].get('message')}")
                return m.get("result") or {}
            self._event(m)

    def send(self, method, params=None):
        """Without waiting for a reply (a frame acknowledgement in the middle of someone else's call)."""
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}, "sessionId": self.session}))

    def _event(self, m):
        if m.get("method") and self.on_event:
            self.on_event(m["method"], m.get("params") or {})
        if m.get("method") == "Page.loadEventFired":
            self.loaded = True

    def pump(self, seconds):
        """Listen for events for this many seconds (while nothing happens in the video, frames keep coming anyway)."""
        end = time.time() + seconds
        while (left := end - time.time()) > 0:
            self.ws.sock.settimeout(min(left, 1))
            try:
                self._event(json.loads(self.ws.recv()))
            except socket.timeout:
                pass
            finally:
                self.ws.sock.settimeout(self.timeout)

    def js(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            text = (d.get("exception") or {}).get("description") or d.get("text") or i18n.t("cli.proof.page_error")
            raise Error(text.splitlines()[0].removeprefix("Error: "))   # without the script's stack
        return (r.get("result") or {}).get("value")

    def viewport(self, width, height, scale=1, mobile=False):
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": mobile})
        self.call("Emulation.setTouchEmulationEnabled", {"enabled": mobile})

    def goto(self, url, wait=0.4):
        """Open and wait for load (no longer than the timeout), then wait more seconds for data the page pulls in by script."""
        self.loaded = False
        self.call("Page.enable")
        if url == getattr(self, "url", None):
            # the same address (the next width): with # the navigation is within the page, no load event, we would wait until the timeout
            # (06.10, `hub shot ...#/s/x --widths 1440,390`), so reload
            self.call("Page.reload", {})
        else:
            r = self.call("Page.navigate", {"url": url})
            if r.get("errorText"):
                raise Error(i18n.t("cli.proof.open_failed", url=url, error=r["errorText"]))
        self.url = url
        end = time.time() + self.timeout
        while not self.loaded and time.time() < end:
            self.pump(0.2)
        if not self.loaded:
            raise Error(i18n.t("cli.proof.load_timeout", seconds=self.timeout, url=url))
        self.pump(wait)

    def close(self):
        atexit.unregister(self.close)
        if getattr(self, "ws", None):
            self.ws.close()
            self.ws = None
        if not hasattr(self, "proc"):   # Chrome was never started
            shutil.rmtree(self.dir, ignore_errors=True)
            return
        mermaid.stop(self.proc)
        litter.own(self.dir)   # its com.google.Chrome.* directory is in the system temp folder, outside the profile
        shutil.rmtree(self.dir, ignore_errors=True)


def url_of(target):
    """An address or a file path -> URL (a local HTML file of its own too)."""
    if "://" in target or target.startswith(("about:", "data:")):
        return target
    import pathlib
    return pathlib.Path(os.path.abspath(target)).as_uri()
