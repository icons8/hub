"""The machine's proxy in tests, only on 127.0.0.1: the environment that names it for every address, and a proxy that
itself listens on TLS (HTTPS_PROXY=https://…) as urllib meets it (2026-11-05): urllib sends it a bare CONNECT, and it
answers with a fatal TLS alert, not HTTP — urllib read that as a status line (http.client.BadStatusLine)."""
import os
import socket
import threading
from unittest import mock

TLS_ALERT = b"\x15\x03\x03\x00\x02\x02\x16"


def proxied(url):
    """The machine's proxy for every address, in both spellings (POSIX reads both, lowercase wins)."""
    env = {"https_proxy": url, "http_proxy": url, "all_proxy": "", "no_proxy": ""}
    return mock.patch.dict(os.environ, {**env, **{k.upper(): v for k, v in env.items()}})


def alert_proxy(test):
    """A proxy on 127.0.0.1 that answers every connection with TLS_ALERT, whatever comes first (a bare CONNECT, a TLS
    hello), and closes gracefully; its address https://127.0.0.1:<port>. It closes with the test."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    test.addCleanup(srv.close)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return   # the test closed it
            with conn:
                try:
                    conn.settimeout(10)
                    conn.recv(4096)
                    conn.sendall(TLS_ALERT)
                    # a graceful close, as OpenSSL does: the alert, then FIN, then whatever the client still sends is
                    # read away — a close with unread bytes would be a reset, which may overtake the alert, and the
                    # client would see "connection reset" instead of the alert (green on the old code for no reason)
                    conn.shutdown(socket.SHUT_WR)
                    while conn.recv(4096):
                        pass
                except OSError:
                    pass
    threading.Thread(target=serve, daemon=True).start()
    return f"https://127.0.0.1:{srv.getsockname()[1]}"
