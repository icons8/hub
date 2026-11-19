"""Port forwarding into the tailnet (TCP): for pages that listen only on 127.0.0.1 and cannot be reworked, i.e. the
direction boards of earlier versions (WebSocket, absolute paths: an HTTP proxy with a prefix breaks them). Listens on
the host's tailnet IP on the same port, lets in only whoever allow(address) permits (the host's owner, by whois,
see access.py), and pipes the bytes to 127.0.0.1.

Host: the board does not check it itself, and a site opened on the owner's phone could reach it through DNS rebinding
(its own name pointing at the node's tailnet IP), while the board's tasks are read by sessions. With hosts, the first
request of every connection must name one of them; the browser keeps a connection per site, so one started under the
node's name stays under it."""
import socket
import threading

HEAD_MAX = 64 * 1024
REFUSED = (b"HTTP/1.1 421 Misdirected Request\r\nContent-Type: text/plain\r\nContent-Length: 22\r\n"
           b"Connection: close\r\n\r\nnot this server's name")


def request_host(head):
    """The Host header from the head of an HTTP request (bytes up to the blank line); None if it is missing or there are several."""
    lines = head.split(b"\r\n")
    hosts = [ln.split(b":", 1)[1].strip() for ln in lines[1:] if ln[:5].lower() == b"host:"]
    if len(hosts) != 1:
        return None
    return hosts[0].decode("ascii", "replace").lower()


class Forward:
    def __init__(self, bind, port, target, allow, stop=None, hosts=None):
        self.bind, self.port, self.target, self.allow = bind, port, target, allow
        self.hosts = hosts   # None: any; otherwise a function returning the allowed Hosts ("ip:port")
        self.stop = stop or threading.Event()

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(32)
        srv.settimeout(1)
        try:
            while not self.stop.is_set():
                try:
                    conn, (peer, _) = srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.one, args=(conn, peer), daemon=True).start()
        finally:
            srv.close()

    def one(self, conn, peer):
        try:
            if not self.allow(peer):
                return conn.close()
            head = b""
            if self.hosts is not None:
                conn.settimeout(10)
                while b"\r\n\r\n" not in head and len(head) < HEAD_MAX:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return conn.close()
                    head += chunk
                if request_host(head.split(b"\r\n\r\n", 1)[0]) not in {h.lower() for h in self.hosts()}:
                    conn.sendall(REFUSED)
                    return conn.close()
                conn.settimeout(None)
            up = socket.create_connection(("127.0.0.1", self.target), timeout=10)
            up.sendall(head)
        except OSError:
            return conn.close()
        up.settimeout(None)
        threading.Thread(target=pipe, args=(conn, up), daemon=True).start()
        pipe(up, conn)


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
