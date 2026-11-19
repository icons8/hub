"""Base of the hub's HTTP servers: threads and bind without asking the name service who this address is.

http.server calls socket.getfqdn(host) on bind — a reverse lookup nobody needs here (nobody reads server_name). Where
the resolver is slow (14.10: a GitHub Mac runner — tens of seconds), the server would take as long to start.
The response goes without Nagle delay: headers and body go out in two writes, and the second otherwise waits for the
first to be acknowledged, while the browser delays that — ~40 ms per request on an open connection (18.10).

Under overload (25.10: load 120–250, swap full) a request thread waits for a lock or the GIL for minutes, the client
leaves on its own timeout and retries, and the departed client's socket stays open until the thread finishes: in ~15
min the page collected 289 descriptors with a soft limit of 256 (CLOSED and CLOSE_WAIT), then "Too many open files" and
a drop of any request. Hence:
- connections per process — at most MAX_CONNECTIONS: each server (the page listens on both localhost and tailnet) gets
  its own half, extras get an immediate 503 with Retry-After from the accept thread. A slot is taken before checking
  whether it is the owner (the handler does that): another tailnet node would take the shared budget whole and shut the
  page out on localhost too;
- a silent connection (keep-alive of a departed phone, a write to a client that does not read) is closed after conn_timeout;
- gone(): a long wait inside a request checks whether the client is still waiting (Gone — drop the request and close the socket)."""
import io
import selectors
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from core import nolookup

MAX_CONNECTIONS = 96   # per process: the soft file limit under launchd is 256, the rest is journal, modules, git, pipes
BUSY = b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
# a refused connection is read to its end off the accept thread (27.10): a close with the request unread sends a reset,
# and Windows drops the 503 the client already has; the request may come after the refusal or in a second segment
DRAIN_FOR = 2.0   # seconds at most per refused connection
DRAIN_MAX = 32    # at once: descriptors are what runs out under overload; beyond that it is closed at once, as in v1.4.0


class Drain:
    """One background thread per process reads refused connections until the client closes or DRAIN_FOR passes, then
    closes them; the accept thread only hands them over and never waits."""

    def __init__(self):
        self.lock = threading.Lock()
        self.more = threading.Event()
        self.new, self.count, self.thread = [], 0, None

    def add(self, sock):
        with self.lock:
            if self.count >= DRAIN_MAX:
                return False
            self.count += 1
            self.new.append(sock)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="httpd-drain", daemon=True)
                self.thread.start()
        self.more.set()
        return True

    def _run(self):
        sel = selectors.DefaultSelector()
        try:
            self._loop(sel)
        except BaseException as e:  # noqa: BLE001 — a dead thread would keep the count at DRAIN_MAX and the sockets open
            sys.stderr.write(f"httpd drain: {e!r}\n")
            try:
                held = [key.fileobj for key in sel.get_map().values()]
            except Exception:  # noqa: BLE001
                held = []
            with self.lock:   # the next add starts a new thread
                held += self.new
                self.new, self.count, self.thread = [], 0, None
            for sock in held:
                try:
                    sock.close()
                except OSError:
                    pass

    def _loop(self, sel):
        while True:
            with self.lock:
                new, self.new = self.new, []
            for sock in new:
                try:
                    sel.register(sock, selectors.EVENT_READ, time.monotonic() + DRAIN_FOR)
                except (OSError, ValueError):   # not registered (the select limit on Windows) — closed at once
                    self._done(sock)
            if not sel.get_map():
                self.more.wait()
                self.more.clear()
                continue
            ready = {key.fileobj for key, _ in sel.select(0.05)}
            now, empty = time.monotonic(), False
            for key in list(sel.get_map().values()):
                sock, done = key.fileobj, now >= key.data
                if sock in ready:
                    try:
                        done = done or not sock.recv(65536)   # b"" — the client closed
                    except BlockingIOError:
                        empty = True   # reported ready, nothing to read
                    except OSError:
                        done = True
                if done:
                    sel.unregister(sock)
                    self._done(sock)
            if empty:
                time.sleep(0.05)   # select would report it again at once: no hot loop until the deadline

    def _done(self, sock):
        sock.close()
        with self.lock:
            self.count -= 1


DRAIN = Drain()


CHUNK = 64 << 10


class Writer(io.BufferedIOBase):
    """The handler's wfile that writes the response in pieces: the socket timeout (conn_timeout) applies to sendall for
    the whole call (Python 3.5+), and a multi-megabyte response (snapshot, mermaid) over a slow link (a phone on the
    tailnet) would be cut off in the middle. In pieces the timeout applies per CHUNK: a departed or non-reading client
    releases the thread, a slow one finishes reading."""

    def __init__(self, sock):
        self._sock = sock

    def writable(self):
        return True

    def write(self, b):
        with memoryview(b) as view:
            view = view.cast("B")
            for at in range(0, len(view), CHUNK):
                self._sock.sendall(view[at:at + CHUNK])
            return len(view)

    def fileno(self):
        return self._sock.fileno()


class Gone(ConnectionAbortedError):
    """The client left while the request waited: nobody to answer — the thread drops the request, the socket is closed (quietly, like a disconnect)."""


def gone(sock):
    """Did the client close the connection (FIN) or reset it (RST)? Without waiting; it does not touch bytes that
    arrived (the next request on keep-alive)."""
    was = sock.gettimeout()
    try:
        sock.settimeout(0)
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True
    finally:
        try:
            sock.settimeout(was)
        except OSError:
            pass


class Server(nolookup.NoLookup, ThreadingHTTPServer):
    daemon_threads = True
    # queue of not-yet-accepted connections (socketserver has 5): under overload the accept thread waits for the GIL, and
    # the sixth client got a reset within milliseconds (25.10) instead of waiting its turn. It spends no process descriptors
    request_queue_size = 64
    conn_timeout = 60.0   # seconds without a byte in either direction (as for module proxies, modproxy.IDLE)

    def get_request(self):
        conn, addr = super().get_request()
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        conn.settimeout(self.conn_timeout)
        return conn, addr

    def slots(self):
        """This server's slots — half of MAX_CONNECTIONS (localhost and tailnet each get their own); only the accept
        thread asks, created at the first connection."""
        if "_slots" not in vars(self):
            self._slots = threading.BoundedSemaphore(max(1, MAX_CONNECTIONS // 2))
        return self._slots

    def process_request(self, request, client_address):
        slots = self.slots()
        if not slots.acquire(blocking=False):
            return self.refuse(request)
        try:
            super().process_request(request, client_address)
        except BaseException:   # the thread did not start — the slot is not taken
            slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)   # closes the socket in finally
        finally:
            self._slots.release()

    def refuse(self, request):
        """503 from the accept thread at once, without a new thread and without waiting: the response and the end of the
        writing side; the rest of the connection is read to its end by DRAIN (otherwise close sends RST instead of the response)."""
        try:
            request.setblocking(False)
            try:
                request.recv(65536)
            except OSError:
                pass
            request.send(BUSY)
            request.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        else:
            if DRAIN.add(request):
                return
        self.shutdown_request(request)
