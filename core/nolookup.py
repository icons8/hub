"""Bind a server without asking the name service who its address is.

http.server calls socket.getfqdn(host) on bind — a reverse lookup nobody needs here (nobody reads server_name); where the
resolver is slow the server would take as long to start (core/httpd.py, 14.10). A mixin before an http.server class:
`class S(nolookup.NoLookup, ThreadingHTTPServer)`. Its own file: a page server of the previous release imports the gateway
lazily from the new tree on disk, while its core.httpd is the old one in memory and has no new names."""
import socketserver


class NoLookup:
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
